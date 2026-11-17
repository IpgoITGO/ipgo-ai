"""#122 연결: 합성 모델 예측과 가짜 LLM. 유료 호출/실제 weight 검증과 구분한다."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import calculation
from engine.model19 import Prediction
from insight.llm import FakeClient, LLMTimeout
from insight.model_prompt import MODEL_PROMPT_VERSION, MODEL_SYSTEM_PROMPT
from insight.service import generate_insight
from schemas.engine import ProductResult
from schemas.insight import InsightInput
from tests.insight_fallback import build_model_fallback
from tests.test_model19 import inputs


@pytest.fixture
def model_result(monkeypatch):
    monkeypatch.setenv("IPGO_MODEL_INSIGHT_ENABLED", "true")
    bundle = SimpleNamespace(version="test-version", predict=lambda *a: Prediction(19, 24, 30, 4, 28, False))
    monkeypatch.setattr("engine.model19.load_bundle", lambda *a: bundle)
    req, daily = inputs(plannedReceipts=[dict(productId=7, quantity=3, expectedOn="2026-11-12")])
    return calculation.calculate(req, daily).results[0]


def example():
    return next(line for line in MODEL_SYSTEM_PROMPT.splitlines() if line.startswith('{"sentences"'))


def test_model_facts_have_honest_contract_without_numeric_values(model_result):
    r = model_result
    assert r.recommended_qty == 23  # 목표28 - 재고2 - 기존발주3
    assert r.risk_status is None
    inp = r.insight_input
    assert inp.risk_status == "NOT_COMPUTED" and inp.method.value == "LGBM19"
    assert "target_stock" in inp.placeholders and "multiplier" not in inp.placeholders
    assert not any(k.startswith("risk_") for k in inp.placeholders)
    assert ProductResult.model_validate(r.to_json_dict()) == r
    assert "28" not in json.dumps(inp.to_json_dict())


def test_model_request_uses_model_prompt_and_version_on_success_and_timeout(model_result):
    client = FakeClient([example()])
    out = generate_insight(model_result.insight_input, client)
    assert out.validation.value == "PASSED" and out.prompt_version == MODEL_PROMPT_VERSION
    assert client.calls[0][0] == MODEL_SYSTEM_PROMPT
    failed = generate_insight(model_result.insight_input, FakeClient([LLMTimeout("synthetic", 25)]))
    assert failed.fail_reason.value == "LLM_TIMEOUT" and failed.prompt_version == MODEL_PROMPT_VERSION


def test_rule_valid_but_wrong_sample_reason_fails_model_semantic_guard(model_result):
    wrong = example().replace("이 예측 방식에서는 품절 위험을 산출하지 않았습니다.",
                              "품절 위험은 표본이 부족해 산출하지 않았습니다.")
    out = generate_insight(model_result.insight_input, FakeClient([wrong]))
    assert out.validation.value == "FAILED" and out.fail_reason.value == "VALIDATION_FAILED"


def test_model_contract_cannot_claim_a_sample_status(model_result):
    data = model_result.insight_input.to_json_dict() | {"riskStatus": "INSUFFICIENT_SAMPLE"}
    with pytest.raises(ValidationError):
        InsightInput.model_validate(data)


def test_model_fixed_text_uses_only_model_facts(model_result):
    fixed = build_model_fallback(model_result)
    assert fixed is not None
    assert "목표 재고 28개" in fixed.text and "권장 입고량은 23개" in fixed.text
    assert "품절 위험을 산출하지 않았습니다" in fixed.text
    assert not any(word in fixed.text for word in ("계산식", "여유 배수", "표본", "확률"))


def test_model_http_disabled_before_call(client, monkeypatch, model_result):
    from app.main import app
    from app.routers.insight import get_llm_client
    fake = FakeClient([example()])
    app.dependency_overrides[get_llm_client] = lambda: fake
    monkeypatch.setenv("IPGO_MODEL_INSIGHT_ENABLED", "false")
    try:
        res = client.post("/engine/insight", json={"resultId": 1, "input": model_result.insight_input.to_json_dict()})
        assert res.status_code == 409 and not fake.calls
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
