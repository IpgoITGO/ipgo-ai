"""판사 전용 LLM 호출기 (#54 "판사 전용 호출기를 평가 코드 안에 새로 추가").

insight/llm.py의 LLMClient 규약 · LLMResult · LLMError를 가져다 쓰고, insight/llm.py는 바꾸지 않는다.
#54 11/3 시험 호출(gpt-5.6-sol)에서 확인한 것:
  - max_tokens는 400 오류 ("Use 'max_completion_tokens' instead") → max_completion_tokens로 보낸다
  - temperature 0은 400 오류 ("Only the default (1) value is supported") → temperature를 보내지 않는다
단가는 공식 가격표로 확인하지 않아 코드에 두지 않는다. 참조 구현 judge와 같이 JUDGE_PRICE_IN_PER_1M ·
JUDGE_PRICE_OUT_PER_1M 환경 변수가 있으면 그 값으로 비용을 계산한다.
"""

import os
import time

import openai

from insight.llm import LLMError, LLMResult, LLMTimeout

DEFAULT_TIMEOUT_SEC = 60.0


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def judge_cost(input_tokens: int, output_tokens: int) -> float | None:
    pin, pout = os.getenv("JUDGE_PRICE_IN_PER_1M"), os.getenv("JUDGE_PRICE_OUT_PER_1M")
    if not pin or not pout:
        return None
    return round((input_tokens * float(pin) + output_tokens * float(pout)) / 1e6, 6)


class JudgeClient:
    def __init__(self, model: str, timeout_sec: float | None = None, api_key: str | None = None, sdk=None):
        self.model = model
        self.timeout_sec = timeout_sec or DEFAULT_TIMEOUT_SEC
        # SDK 자체 재시도를 끈다 (insight/llm.py와 같음). sdk는 테스트용 가짜 객체
        self._client = sdk or openai.OpenAI(
            api_key=api_key or os.getenv("OPENAI_API_KEY"), timeout=self.timeout_sec, max_retries=0
        )

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1200, temperature: float | None = None
    ) -> LLMResult:
        """temperature는 받기만 하고 보내지 않는다 (LLMClient 규약과 맞추기 위한 인자)."""
        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_completion_tokens=max_tokens,
            )
        except openai.APITimeoutError as e:
            raise LLMTimeout(str(e), _elapsed_ms(t0)) from e
        except openai.OpenAIError as e:
            raise LLMError(f"{type(e).__name__}: {e}", _elapsed_ms(t0)) from e
        choice = resp.choices[0]
        it = getattr(resp.usage, "prompt_tokens", 0) or 0
        ot = getattr(resp.usage, "completion_tokens", 0) or 0
        return LLMResult(
            (choice.message.content or "").strip(),
            resp.model or self.model,
            it,
            ot,
            _elapsed_ms(t0),
            judge_cost(it, ot),
            choice.finish_reason == "length",
        )
