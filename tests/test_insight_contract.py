"""#10 전송 계약: mock 연동·개인정보 경계·메타데이터. 수량 기대값은 기존 DA fixture를 유지한다."""
import copy
import json
from datetime import date

import pytest
from pydantic import ValidationError

from app.main import app
from app.routers.insight import get_llm_client
from engine.calculate import calculate, custom_scenario
from insight.llm import FakeClient, LLMError, LLMTimeout
from insight.prompt import PROMPT_VERSION, allowed_keys, required_keys
from insight.validator import check_draft
from schemas.base import Method
from schemas.engine import CalculateRequest, ProductResult, ScenarioRequest
from schemas.insight import InsightInput
from tests.payloads import CALC_REQUEST, INSIGHT_REQUEST
from tests.test_calculate import daily_rows, request_for


def test_mock_calculation_input_round_trips_into_insight(client):
    result = client.post("/engine/calculate", json=CALC_REQUEST).json()["results"][0]
    inp = InsightInput.model_validate(result["insightInput"])
    assert isinstance(inp.placeholders, list) and inp.risk_status == result["riskStatus"]
    assert inp.scenarios == ["P80", "P100", "P120"]
    assert result["riskBasis"] == "SKU" and result["noDateReceiptQty"] == 0
    response = client.post("/engine/insight", json={"resultId": 9001, "input": result["insightInput"]})
    assert response.status_code == 200
    body = response.json()
    assert body["promptVersion"] == "mock" and body["validation"] == "PASSED"
    assert check_draft(body["draft"], allowed_keys(inp), required_keys(inp)).ok
    assert not {"mode", "templateVersion", "fallbackDraft", "failDetail"} & body.keys()


@pytest.mark.parametrize("change", [
    {"placeholders": {"qty_p100": "1,240"}},  # 실제 값 dict는 받지 않는다.
    {"riskStatus": None},                   # 미구현을 표본 부족으로 치환하지 않는다.
    {"directions": {"ending_p80_vs_p100": "늘지만"}},
    {"status": "OK"},                      # 정상 여부 판정과 호출 범위는 Spring 책임.
    {"scenarios": ["P100"]},               # 이름 목록에 남은 P80/P120과 서로 다르면 거부.
    {"scenarios": ["P80", "P100", "P120", "P100"]},
])
def test_input_rejects_values_unknown_state_or_direction(client, change):
    payload = copy.deepcopy(INSIGHT_REQUEST)
    payload["input"].update(change)
    assert client.post("/engine/insight", json=payload).status_code == 422


def test_structured_input_keeps_names_and_calculation_flags():
    payload = {**INSIGHT_REQUEST["input"], "scenarios": ["P80", "P100", "P120"],
               "methodFallback": True, "startStockEstimated": True, "hasNoDateReceipts": True}
    out = InsightInput.model_validate(payload).to_json_dict()
    assert out["scenarios"] == ["P80", "P100", "P120"]
    assert out["methodFallback"] and out["startStockEstimated"] and out["hasNoDateReceipts"]
    assert isinstance(out["placeholders"], list)


@pytest.mark.parametrize("receipt", [
    {"productId": 101, "quantity": 0},
    {"productId": 101, "quantity": -1},
    {"productId": 101, "quantity": 1.5},
    {"productId": 101, "quantity": True},
    {"productId": 101, "quantity": "3"},
    {"productId": 0, "quantity": 1},
    {"productId": True, "quantity": 1},
])
def test_no_date_receipts_require_positive_product_and_whole_quantity(receipt):
    with pytest.raises(ValidationError):
        CalculateRequest.model_validate({**CALC_REQUEST, "noDateReceipts": [receipt]})


def test_no_date_receipts_are_metadata_and_do_not_change_calculation():
    as_of = date(2026, 10, 14)
    request = request_for(as_of, as_of, 10, 3)
    daily = daily_rows(101, as_of, 2, 10)
    baseline = calculate(request, daily).results[0]
    payload = request.to_json_dict()
    payload["noDateReceipts"] = [{"productId": 101, "quantity": 2}, {"productId": 101, "quantity": 3},
                                 {"productId": 202, "quantity": 99}]
    changed_request = CalculateRequest.model_validate(payload)
    changed = calculate(changed_request, daily).results[0]
    assert changed.no_date_receipt_qty == 5
    assert baseline.model_dump(exclude={"no_date_receipt_qty"}) == changed.model_dump(exclude={"no_date_receipt_qty"})
    before = custom_scenario(ScenarioRequest(result_id=1, calculation_input=request, qty=15), daily)
    after = custom_scenario(ScenarioRequest(result_id=1, calculation_input=changed_request, qty=15), daily)
    assert before == after
    assert changed.insight_input is None and changed.risk_basis is None and changed.risk_status is None


def test_method_fallback_reflects_selected_forecast():
    as_of = date(2026, 10, 14)
    request = request_for(as_of, as_of, 10, 3)
    daily = daily_rows(101, as_of, 2, 10)
    assert calculate(request, daily).results[0].method_fallback is False
    payload = request.to_json_dict()
    payload["policy"]["defaultMethod"] = "A"
    result = calculate(CalculateRequest.model_validate(payload), daily).results[0]
    assert result.method == Method.B and result.method_fallback is True


def test_non_ok_result_cannot_publish_insight_input():
    with pytest.raises(ValidationError):
        ProductResult(product_id=101, status="NOT_NEEDED", insight_input=INSIGHT_REQUEST["input"])


def test_insight_input_cannot_fabricate_missing_risk_status():
    with pytest.raises(ValidationError):
        ProductResult(product_id=101, status="OK", insight_input=INSIGHT_REQUEST["input"])


@pytest.mark.parametrize("reply", [
    json.dumps({"sentences": [
        "권장량 {{qty_p100}}개의 도착 후 품절 위험은 {{risk_basis}} 추정치로 {{risk_post_p100}}입니다.",
        "도착 전 품절 위험 추정치는 {{risk_pre}}입니다."]}, ensure_ascii=False),
    "not json",
    LLMTimeout("slow", 10000),
    LLMError("unavailable", 10),
], ids=["passed", "validation_failed", "timeout", "call_error"])
def test_real_success_and_failure_responses_include_prompt_version(client, monkeypatch, reply):
    monkeypatch.setenv("ENGINE_MODE", "real")
    app.dependency_overrides[get_llm_client] = lambda: FakeClient([reply])
    try:
        response = client.post("/engine/insight", json=INSIGHT_REQUEST)
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert response.status_code == 200, response.text
    assert response.json()["promptVersion"] == PROMPT_VERSION
    assert "mode" not in response.json()
