"""AI-02 LLM 호출기 (#32). 한 번만 부르고(단일 호출) 호출 오류와 시간 초과를 구분한다.

설정 (환경 변수. .env.example 반영은 PR #20 병합 뒤)
  OPENAI_API_KEY   실제 호출 시 필수
  OPENAI_MODEL     기본 gpt-4o-mini
  LLM_TIMEOUT_SEC  기본 10 (명세 5장 가정값, 7장 미결)
"""
import os
import time
from dataclasses import dataclass
from typing import Protocol

import openai

from schemas.insight import FailReason

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_SEC = 10.0
# USD / 1M tokens (입력, 출력). 10/21 기록의 비용과 맞는 값. 측정 날 OpenAI 가격표로 다시 확인한다.
PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


class LLMError(Exception):
    """호출 오류(네트워크 · 제공사 오류 · 한도). 실패해도 걸린 시간을 남긴다."""

    reason = FailReason.LLM_ERROR

    def __init__(self, message: str, latency_ms: int):
        super().__init__(message)
        self.latency_ms = latency_ms


class LLMTimeout(LLMError):
    reason = FailReason.LLM_TIMEOUT


@dataclass
class LLMResult:
    text: str
    model: str            # 응답이 알려 준 모델 이름 (스냅샷 포함)
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float | None  # 단가를 모르는 모델이면 None
    truncated: bool       # max_tokens에서 잘림


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, user: str, *, max_tokens: int = 400, temperature: float = 0.2) -> LLMResult: ...


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICES_PER_1M:
        return None
    pin, pout = PRICES_PER_1M[model]
    return round((input_tokens * pin + output_tokens * pout) / 1e6, 6)


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


class OpenAIClient:
    def __init__(self, model: str | None = None, timeout_sec: float | None = None, api_key: str | None = None):
        self.model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        self.timeout_sec = timeout_sec or float(os.getenv("LLM_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))
        # SDK 자체 재시도를 끈다 (단일 호출)
        self._client = openai.OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"), timeout=self.timeout_sec,
                                     max_retries=0)

    def complete(self, system: str, user: str, *, max_tokens: int = 400, temperature: float = 0.2) -> LLMResult:
        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},  # 시험 prompt 고정본의 JSON 출력. 운영 형식은 ipgo-server#25
            )
        except openai.APITimeoutError as e:
            raise LLMTimeout(str(e), _elapsed_ms(t0)) from e
        except openai.OpenAIError as e:
            raise LLMError(f"{type(e).__name__}: {e}", _elapsed_ms(t0)) from e
        latency = _elapsed_ms(t0)
        choice = resp.choices[0]
        it = getattr(resp.usage, "prompt_tokens", 0) or 0
        ot = getattr(resp.usage, "completion_tokens", 0) or 0
        return LLMResult((choice.message.content or "").strip(), resp.model or self.model, it, ot, latency,
                         estimate_cost(self.model, it, ot), choice.finish_reason == "length")


class FakeClient:
    """테스트 · --fake용. 응답 글자를 순서대로 돌려주고, 예외가 들어 있으면 그 예외를 낸다."""

    def __init__(self, responses: list[str | Exception] | None = None, model: str = "fake-model",
                 latency_ms: int = 1):
        self.model = model
        self.latency_ms = latency_ms
        self._responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 400, temperature: float = 0.2) -> LLMResult:
        self.calls.append((system, user))
        r = self._responses.pop(0) if self._responses else ""
        if isinstance(r, Exception):
            raise r
        it, ot = (len(system) + len(user)) // 4, len(r) // 2
        return LLMResult(r, self.model, it, ot, self.latency_ms, estimate_cost(self.model, it, ot), False)
