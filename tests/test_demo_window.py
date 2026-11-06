from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from insight.demo_window import SNAPSHOT, options
from insight.llm import LLMError, OpenAIClient

END = "2026-11-06T08:30:00Z"


def test_continuous_mode_keeps_snapshot_and_limits_without_expiry(monkeypatch):
    monkeypatch.delenv("IPGO_LLM_EXPIRES_AT", raising=False)
    monkeypatch.setenv("IPGO_LLM_CONTINUOUS_ENABLED", "true")
    value = options("system", "user", 400, "gpt-4o-mini")
    assert value == {"model": SNAPSHOT, "n": 1, "service_tier": "default"}
    for text, output, model in [("한" * 6000, 400, "gpt-4o-mini"), ("x", 401, "gpt-4o-mini"), ("x", 400, "gpt-4o")]:
        with pytest.raises(ValueError):
            options(text, "user", output, model)


def test_continuous_approval_does_not_bypass_explicit_expiry(monkeypatch):
    monkeypatch.setenv("IPGO_LLM_CONTINUOUS_ENABLED", "true")
    monkeypatch.setenv("IPGO_LLM_EXPIRES_AT", "2000-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        options("s", "u", 400, "gpt-4o-mini")


def test_continuous_sdk_calls_once_and_rejects_oversized_input_before_request(monkeypatch):
    monkeypatch.setenv("IPGO_LLM_CONTINUOUS_ENABLED", "true")
    monkeypatch.delenv("IPGO_LLM_EXPIRES_AT", raising=False)
    client = OpenAIClient(api_key="fake-key")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2), model=SNAPSHOT)

    monkeypatch.setattr(client._client.chat.completions, "create", create)
    client.complete("system", "user")
    with pytest.raises(LLMError):
        client.complete("한" * 6000, "user")
    assert len(calls) == 1
    assert (calls[0]["model"], calls[0]["service_tier"], calls[0]["n"]) == (SNAPSHOT, "default", 1)
    assert client._client.max_retries == 0
    assert client._client._client.follow_redirects is False
    client._client.close()


@pytest.mark.parametrize(("at", "allowed"), [("08:29:59", True), ("08:30:00", False), ("08:30:01", False)])
def test_exact_cutoff(monkeypatch, at, allowed):
    monkeypatch.setenv("IPGO_LLM_EXPIRES_AT", END)
    now = datetime.fromisoformat(f"2026-11-06T{at}+00:00")
    if allowed:
        assert options("system", "user", 400, "gpt-4o-mini", now=now)["model"] == SNAPSHOT
    else:
        with pytest.raises(ValueError):
            options("system", "user", 400, "gpt-4o-mini", now=now)


@pytest.mark.parametrize("expiry", ["bad", "2026-11-06T08:30:00", "2000-01-01T00:00:00Z"])
def test_invalid_or_expired_sends_no_sdk_call(monkeypatch, expiry):
    monkeypatch.setenv("IPGO_LLM_EXPIRES_AT", expiry)
    c = OpenAIClient(api_key="fake-key")
    calls = []
    monkeypatch.setattr(c._client.chat.completions, "create", lambda **kw: calls.append(kw))
    with pytest.raises(LLMError):
        c.complete("system", "user")
    assert calls == []
    c._client.close()


@pytest.mark.parametrize(("system", "tokens", "model"), [("한"*6000, 400, "gpt-4o-mini"),
    ("s", 401, "gpt-4o-mini"), ("s", 400, "gpt-4o")])
def test_input_output_model_limits(monkeypatch, system, tokens, model):
    monkeypatch.setenv("IPGO_LLM_EXPIRES_AT", END)
    with pytest.raises(ValueError):
        options(system, "user", tokens, model, now=datetime(2026, 11, 6, 8, 0, tzinfo=UTC))


def test_active_sdk_uses_snapshot_standard_one_choice_no_retry(monkeypatch):
    monkeypatch.setenv("IPGO_LLM_EXPIRES_AT", "2099-01-01T00:00:00Z")
    c = OpenAIClient(api_key="fake-key")
    calls = []
    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2), model=SNAPSHOT)
    monkeypatch.setattr(c._client.chat.completions, "create", create)
    c.complete("system", "user")
    assert len(calls) == 1
    assert (calls[0]["model"], calls[0]["service_tier"], calls[0]["n"], calls[0]["max_tokens"]) == (
        SNAPSHOT, "default", 1, 400
    )
    assert c._client.max_retries == 0
    assert c._client._client.follow_redirects is False
    c._client.close()
