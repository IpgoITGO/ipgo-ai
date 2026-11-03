"""#36 변환 검증. 기존 #44 합성 결과를 재사용하며 DA-07/13 산출이나 실제 LLM 성공을 뜻하지 않는다."""
import json
from datetime import date
from pathlib import Path

import pytest

from app.main import app
from app.routers.insight import get_llm_client
from engine import calculate as calc
from engine.insight_facts import build_catalog, to_insight_input
from insight.llm import FakeClient
from insight.prompt import allowed_keys, build_prompt
from schemas.base import ResultStatus, RiskStatus, ScenarioType
from schemas.engine import ProductResult
from schemas.insight import InsightInput
from tests.test_calculate import replenish_case

CASES = json.loads((Path(__file__).parent / "fixtures/insight_fallback.json").read_text())
READY = [c["id"] for c in CASES if c["result"]["status"] == "OK" and c["expected"]]


def load_result(case_id="no_date_receipts"):
    """#10より前の固定例の別Contextを現行ProductResultへ移す。固定例自体は変更しない。"""
    case = next(c for c in CASES if c["id"] == case_id)
    ctx = case["context"]
    result = ProductResult.model_validate({
        **case["result"], "methodFallback": ctx["methodFallback"], "riskBasis": ctx["riskBasis"],
        "noDateReceiptQty": ctx["noDateReceiptQty"],
    })
    as_of = date.fromisoformat(ctx["asOfDate"]) if ctx["asOfDate"] else None
    return result, as_of


@pytest.mark.parametrize("case_id", READY)
def test_existing_ready_results_have_complete_contract_keys(case_id):
    result, as_of = load_result(case_id)
    before = result.model_dump()
    inp = to_insight_input(result, "이 상품", as_of_date=as_of)
    assert inp is not None
    assert InsightInput.model_validate(inp.to_json_dict()) == inp
    assert result.model_dump() == before
    assert inp.recommended == ScenarioType.P100
    assert inp.scenarios == [s.type for s in result.scenarios if not s.is_duplicate]
    keys = set(inp.placeholders)
    assert len(keys) == len(inp.placeholders)
    assert {"qty_p100", "ending_p100", "method"} <= keys
    if not next(s for s in result.scenarios if s.type == ScenarioType.P80).is_duplicate:
        assert {"qty_p80", "ending_p80"} <= keys  # AI53で追加予定のending_p80も既に渡す
        assert "ending_p80_vs_p100" in inp.directions
    if result.risk_status == RiskStatus.OK:
        assert {"risk_pre", "risk_post_p100", "risk_sample_size"} <= keys
        if "qty_p80" in keys:
            assert "risk_post_p80" in keys and "risk_post_p80_vs_p100" in inp.directions
    else:
        assert not any(k.startswith("risk_") for k in keys | set(inp.directions))
    if result.risk_status == RiskStatus.LOW_GRADE:
        assert {"forecast_range_low", "forecast_range_high"} <= keys
    assert not any(k.startswith(("label_", "action_", "dir_")) for k in keys)
    assert not {"formula_desc", "risk_basis"} & keys
    assert "※" not in build_prompt(inp).user
    # 문구 키는 Spring이 채운다. 변환기는 출처 없는 이름을 만들지 않는다.
    assert {"formula_desc", "label_p100"} <= set(allowed_keys(inp))


@pytest.mark.parametrize("status", [s for s in ResultStatus if s != ResultStatus.OK])
def test_every_non_call_status_returns_none(status):
    result, _ = load_result()
    result = result.model_copy(update={"status": status, "reason": "説明可能な理由"})
    assert to_insight_input(result, "이 상품") is None
    assert build_catalog(result) == []


@pytest.mark.parametrize("field", ["risk_status", "method", "method_fallback", "recommended_qty",
                                   "pre_arrival_risk", "risk_basis", "risk_sample_size"])
def test_missing_required_fact_defers_generation(field):
    result, _ = load_result()
    result = result.model_copy(update={field: None})
    assert to_insight_input(result, "이 상품") is None
    assert getattr(result, field) is None  # 누락을 임의 상태나 기본값으로 고치지 않는다


@pytest.mark.parametrize("case_id", ["low_grade", "insufficient_sample"])
def test_uncomputed_risk_is_never_exposed(case_id):
    result, _ = load_result(case_id)
    # 잘못 남은 과거 위험 수치도 riskStatus가 산출 불가이면 키로 내보내지 않는다.
    result.pre_arrival_risk = 0.99
    for scenario in result.scenarios:
        scenario.post_arrival_risk = 0.99
    inp = to_insight_input(result, "이 상품")
    assert inp is not None and inp.risk_status == result.risk_status
    assert not any("risk_" in k for k in inp.placeholders + list(inp.directions))


@pytest.mark.parametrize("change", [{"forecast_range_low": None}, {"forecast_range_high": None},
                                    {"forecast_range_low": 126}, {"forecast_range_low": float("nan")}])
def test_low_grade_without_a_valid_range_is_deferred(change):
    result, _ = load_result("low_grade")
    assert to_insight_input(result.model_copy(update=change), "이 상품") is None


def test_fallback_and_start_stock_require_their_source_facts():
    result, as_of = load_result("pooled_estimated_custom")
    assert as_of is not None
    assert to_insight_input(result, "이 상품") is None
    inp = to_insight_input(result, "이 상품", as_of_date=as_of)
    assert inp.start_stock_estimated and "as_of_date" in inp.placeholders
    fallback = result.model_copy(update={"method_fallback": True, "method_reason": "비공개 사유"})
    inp = to_insight_input(fallback, "이 상품", as_of_date=as_of)
    assert inp.method_fallback and "method_reason" in inp.placeholders
    assert "비공개 사유" not in inp.model_dump_json()
    assert to_insight_input(fallback.model_copy(update={"method_reason": "  "}), "이 상품",
                            as_of_date=as_of) is None
    result.start_stock_estimated = False
    inp = to_insight_input(result, "이 상품", as_of_date=as_of)
    assert not inp.start_stock_estimated and "as_of_date" not in inp.placeholders
    assert "method_reason" not in inp.placeholders


def test_no_date_receipt_changes_only_the_name_and_flag():
    result, _ = load_result()
    baseline = result.model_copy(update={"no_date_receipt_qty": 0})
    with_receipt = to_insight_input(result, "이 상품")
    without = to_insight_input(baseline, "이 상품")
    assert with_receipt.has_no_date_receipts and not without.has_no_date_receipts
    assert set(with_receipt.placeholders) - set(without.placeholders) == {"no_date_receipts"}
    assert with_receipt.directions == without.directions
    assert with_receipt.scenarios == without.scenarios
    assert result.scenarios == baseline.scenarios and result.recommended_qty == baseline.recommended_qty


def test_duplicates_custom_and_stable_scenario_order():
    duplicate, _ = load_result("normal_duplicate")
    inp = to_insight_input(duplicate, "이 상품")
    assert inp.scenarios == [ScenarioType.P100, ScenarioType.P120]
    assert not any("p80" in k for k in inp.placeholders + list(inp.directions))
    custom, as_of = load_result("pooled_estimated_custom")
    inp = to_insight_input(custom, "이 상품", as_of_date=as_of)
    assert {"qty_custom", "diff_custom", "ending_custom", "risk_post_custom"} <= set(inp.placeholders)
    assert inp.directions["ending_custom_vs_p100"] == "높음"
    assert inp.directions["risk_post_custom_vs_p100"] == "낮음"
    custom.scenarios.reverse()
    assert to_insight_input(custom, "이 상품", as_of_date=as_of) == inp
    next(s for s in custom.scenarios if s.type == ScenarioType.CUSTOM).is_duplicate = True
    inp = to_insight_input(custom, "이 상품", as_of_date=as_of)
    assert not any("custom" in k for k in inp.placeholders + list(inp.directions))


@pytest.mark.parametrize("kind", [ScenarioType.P100, ScenarioType.P80])
def test_missing_required_scenario_is_not_inferred(kind):
    result, _ = load_result()
    result.scenarios = [s for s in result.scenarios if s.type != kind]
    assert to_insight_input(result, "이 상품") is None


def test_ambiguous_baseline_or_duplicate_types_are_deferred():
    result, _ = load_result()
    result.scenarios.append(result.scenarios[0].model_copy())
    assert to_insight_input(result, "이 상품") is None
    result, _ = load_result()
    base = next(s for s in result.scenarios if s.type == ScenarioType.P100)
    base.qty += 1
    assert to_insight_input(result, "이 상품") is None
    base.qty = result.recommended_qty
    base.is_duplicate = True
    assert to_insight_input(result, "이 상품") is None


@pytest.mark.parametrize(("ending", "risk", "direction"), [(20.2, .064, "같음"), (19.9, .054, "낮음"),
                                                           (21.0, .065, "높음")])
def test_directions_compare_displayed_stock_and_percent(ending, risk, direction):
    result, _ = load_result()
    base = next(s for s in result.scenarios if s.type == ScenarioType.P100)
    other = next(s for s in result.scenarios if s.type == ScenarioType.P80)
    base.expected_ending_stock, base.post_arrival_risk = 20.8, .061
    other.expected_ending_stock, other.post_arrival_risk = ending, risk
    inp = to_insight_input(result, "이 상품")
    assert inp.directions == {"ending_p80_vs_p100": direction, "risk_post_p80_vs_p100": direction}
    assert base.expected_ending_stock == 20.8 and base.post_arrival_risk == .061
    assert other.expected_ending_stock == ending and other.post_arrival_risk == risk


@pytest.mark.parametrize("field", ["post_arrival_risk", "expected_ending_stock", "unmet_demand_qty"])
def test_scenario_missing_or_nonfinite_facts_defers_generation(field):
    result, _ = load_result()
    setattr(result.scenarios[0], field, None if field == "post_arrival_risk" else float("inf"))
    assert to_insight_input(result, "이 상품") is None


def test_catalog_carries_no_real_values_or_product_identifiers():
    result, _ = load_result()
    result.product_id = 987654321
    result.recommended_amount = 876543210
    result.method_fallback = True
    result.method_reason = "고객 원가 및 원본 상품명은 보낼 수 없음"
    inp = to_insight_input(result, "이 상품")
    assert inp.sku_label == "이 상품"
    assert "987654321" not in inp.model_dump_json() and "876543210" not in inp.model_dump_json()
    assert result.method_reason not in build_prompt(inp).user
    assert "[값:" not in build_prompt(inp).user
    assert to_insight_input(result, "  ") is None
    before = inp.model_dump()
    inp.placeholders.clear()
    assert to_insight_input(result, "이 상품").model_dump() == before  # 호출 사이에서 목록을 공유하지 않는다


@pytest.mark.parametrize("case", ["1", "2", "3", "4"])
def test_real_calculation_calls_converter_and_preserves_uncomputed_risk(monkeypatch, case):
    request, daily, _, _ = replenish_case(case)
    before, source = request.model_dump(), daily.copy(deep=True)
    calls = []
    def record(result, label, **context):
        calls.append((result.product_id, result.status, context["as_of_date"]))
        return to_insight_input(result, label, **context)
    monkeypatch.setattr(calc, "to_insight_input", record)
    result, = calc.calculate(request, daily).results
    assert calls == [(result.product_id, result.status, request.as_of_date)]
    assert result.insight_input is None and result.risk_status is None
    assert request.model_dump() == before
    assert daily.equals(source)


@pytest.mark.parametrize("case_id", READY)
def test_converted_input_reaches_real_http_service_with_fake_llm(client, monkeypatch, case_id):
    result, as_of = load_result(case_id)
    inp = to_insight_input(result, "이 상품", as_of_date=as_of)
    first = "{{method}} 기준 권장 입고량은 {{qty_p100}}개이며 다음 입고 때 {{ending_p100}}개가 남습니다."
    second = "{{risk_basis}} 추정치로 도착 후 위험은 {{risk_post_p100}}이며 공통 위험은 {{risk_pre}}입니다."
    if inp.risk_status == RiskStatus.LOW_GRADE:
        second = "예상 출고량 범위는 {{forecast_range_low}}~{{forecast_range_high}}개입니다."
    elif inp.risk_status == RiskStatus.INSUFFICIENT_SAMPLE:
        second = "과거 예측 표본이 부족해 품절 위험을 산출하지 않았습니다."
    fake = FakeClient([json.dumps({"sentences": [first, second]}, ensure_ascii=False)])
    monkeypatch.setenv("ENGINE_MODE", "real")
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        response = client.post("/engine/insight", json={"resultId": 1, "input": inp.to_json_dict()})
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert response.status_code == 200, response.text
    assert response.json()["validation"] == "PASSED"
    assert len(fake.calls) == 1
    assert fake.calls[0][1].startswith("상품: 이 상품\n") and "[값:" not in fake.calls[0][1]


def test_calculate_response_can_forward_ready_facts_without_changing_quantity(client, monkeypatch):
    """위험 판정만 가짜 공급: DA-07 실행 증거가 아니다. 수량은 기존 DA04 계산을 그대로 실행한다."""
    from app import repository

    request, daily, _, _ = replenish_case("1")
    baseline = calc.calculate(request, daily).results[0]
    product = calc._product

    def with_risk_status(*args):
        result, planned = product(*args)
        result.risk_status = RiskStatus.INSUFFICIENT_SAMPLE  # 테스트의 명시적 가짜 상류 사실
        return result, planned

    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(repository, "load_product_daily", lambda *args, **kwargs: daily)
    monkeypatch.setattr(calc, "_product", with_risk_status)
    calculated = client.post("/engine/calculate", json=request.to_json_dict())
    assert calculated.status_code == 200, calculated.text
    actual, = calculated.json()["results"]
    result = ProductResult.model_validate(actual)
    assert result.insight_input is not None
    assert result.recommended_qty == baseline.recommended_qty and result.scenarios == baseline.scenarios
    inp = result.insight_input
    fake = FakeClient([json.dumps({"sentences": [
        "{{method}} 기준 권장 입고량은 {{qty_p100}}개입니다.",
        "다음 입고 때 {{ending_p100}}개가 남습니다.",
    ]})])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        response = client.post("/engine/insight", json={"resultId": 1, "input": actual["insightInput"]})
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert response.status_code == 200 and response.json()["validation"] == "PASSED"
    assert len(fake.calls) == 1 and inp.sku_label == "이 상품"
