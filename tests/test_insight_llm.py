"""LLM 호출기 (#32). 실제 API를 부르지 않고 SDK 응답 · 예외를 흉내 낸다. 기대값은 이석형이 확인한다."""
from types import SimpleNamespace

import httpx
import openai
import pytest

from insight.llm import FakeClient, LLMError, LLMTimeout, OpenAIClient, estimate_cost
from schemas.insight import FailReason

REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _client(monkeypatch, create, model="gpt-4o-mini"):
    c = OpenAIClient(model=model, api_key="test-key")
    monkeypatch.setattr(c._client.chat.completions, "create", create)
    return c


def _resp(text, finish_reason="stop", it=1000, ot=100, model="gpt-4o-mini-2024-07-18"):
    choice = SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=it, completion_tokens=ot), model=model)


@pytest.mark.parametrize(
    ("model", "it", "ot", "expected"),
    [
        ("gpt-4o-mini", 1000, 100, 0.00021),  # (1000 × 0.15 + 100 × 0.60) / 1M
        ("gpt-4o", 1000, 100, 0.0035),        # (1000 × 2.50 + 100 × 10.00) / 1M
        ("unknown-model", 1000, 100, None),   # 단가를 모르면 비용 없음
    ],
)
def test_estimate_cost(model, it, ot, expected):
    assert estimate_cost(model, it, ot) == expected


def test_success(monkeypatch):
    c = _client(monkeypatch, lambda **kw: _resp('{"sentences": ["a"]}'))
    r = c.complete("sys", "user")
    assert r.text == '{"sentences": ["a"]}'
    assert r.model == "gpt-4o-mini-2024-07-18"  # 요청 이름이 아니라 응답의 스냅샷 이름
    assert (r.input_tokens, r.output_tokens) == (1000, 100)
    assert r.cost_usd == 0.00021
    assert r.truncated is False
    assert r.latency_ms >= 0


def test_truncated(monkeypatch):
    c = _client(monkeypatch, lambda **kw: _resp('{"sentences": ["a', finish_reason="length"))
    assert c.complete("sys", "user").truncated is True


def _raise(exc):
    def create(**kw):
        raise exc
    return create


def test_timeout(monkeypatch):
    c = _client(monkeypatch, _raise(openai.APITimeoutError(request=REQ)))
    with pytest.raises(LLMTimeout) as e:
        c.complete("sys", "user")
    assert e.value.reason == FailReason.LLM_TIMEOUT
    assert e.value.latency_ms >= 0


def test_call_error(monkeypatch):
    c = _client(monkeypatch, _raise(openai.APIConnectionError(message="down", request=REQ)))
    with pytest.raises(LLMError) as e:
        c.complete("sys", "user")
    assert not isinstance(e.value, LLMTimeout)
    assert e.value.reason == FailReason.LLM_ERROR
    assert e.value.latency_ms >= 0


def test_single_call_and_default_timeout(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    c = OpenAIClient(api_key="test-key")
    assert c._client.max_retries == 0  # SDK 재시도 끔
    assert c.timeout_sec == 10.0       # 명세 5장 가정값
    assert c.model == "gpt-4o-mini"


def test_empty_env_values_use_defaults(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SEC", "")  # .env에 "LLM_TIMEOUT_SEC=" 만 있는 경우
    monkeypatch.setenv("OPENAI_MODEL", "")
    c = OpenAIClient(api_key="test-key")
    assert (c.model, c.timeout_sec) == ("gpt-4o-mini", 10.0)


def test_fake_client_raises_queued_error():
    c = FakeClient(["ok", LLMTimeout("slow", 10000)], model="gpt-4o-mini")
    assert c.complete("s", "u").text == "ok"
    with pytest.raises(LLMTimeout):
        c.complete("s", "u")
    assert len(c.calls) == 2
