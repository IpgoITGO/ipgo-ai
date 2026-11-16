"""#119: 목표 재고 28개·현재 재고 2개인 합성 입력의 수량을 손계산으로 대조한다."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import calculation, repository
from app.jobs import JobEnvelope, execute
from engine.model19 import Prediction
from schemas.engine import ScenarioRequest
from tests.test_model19 import inputs


@pytest.fixture
def fixed_model(monkeypatch):
    monkeypatch.setattr("engine.model19.load_bundle", lambda *args: SimpleNamespace(
        version="test-version", predict=lambda *args: Prediction(19, 24, 30, 4, 28, False),
    ))


@pytest.mark.parametrize(("unit", "expected", "scenarios"), [
    (1, 23, [19, 23, 28]), (5, 25, [20, 25, 30]), (7, 28, [28, 28, 35]),
])
def test_http_worker_round_after_subtracting_stock_and_receipts(client, monkeypatch, fixed_model,
                                                              unit, expected, scenarios):
    # 28 - 2 - 3 = 23개를 주문 단위로 올린 뒤, 각 80/100/120%도 같은 단위로 올린다.
    request, daily = inputs(
        settings=[dict(productId=7, leadTimeDays=5, reviewCycleDays=14, orderUnitQty=unit, unitCost=100)],
        plannedReceipts=[dict(productId=7, quantity=3, expectedOn="2026-11-12")],
    )
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(repository, "load_product_daily", lambda *args, **kwargs: daily)
    http = client.post("/engine/calculate", json=request.to_json_dict())
    assert http.status_code == 200
    result = http.json()["results"][0]
    assert result["recommendedQty"] == expected
    assert result["recommendedAmount"] == expected * 100
    assert [s["qty"] for s in result["scenarios"]] == scenarios
    assert result["scenarios"][0]["isDuplicate"] is (unit == 7)
    job = JobEnvelope(contractVersion="1", jobId="synthetic-order-unit", jobType="CALCULATE",
                      payload=request.to_json_dict())
    assert execute(job).result == http.json()


@pytest.mark.parametrize(("days", "expected"), [(0, 26), (19, 23), (20, 26)])
def test_only_future_receipts_inside_19_days_reduce_new_order(fixed_model, days, expected):
    request, daily = inputs()
    receipt = dict(productId=7, quantity=3, expectedOn=request.today + timedelta(days=days))
    request, daily = inputs(plannedReceipts=[receipt])
    result = calculation.calculate(request, daily).results[0]
    assert result.recommended_qty == expected


def test_undated_receipts_do_not_reduce_order(fixed_model):
    request, daily = inputs(noDateReceipts=[dict(productId=7, quantity=100)])
    result = calculation.calculate(request, daily).results[0]
    assert result.recommended_qty == 26 and result.no_date_receipt_qty == 100


def test_empty_stock_does_not_subtract_prearrival_unmet_again(fixed_model):
    request, daily = inputs()
    daily["closing_qty"] = 0.
    result = calculation.calculate(request, daily).results[0]
    assert result.recommended_qty == 28  # BUFFER100_FLOOR0: 28 - 0, 도착 전 5개 손실을 빼지 않는다.


def test_stock_above_target_needs_no_order(fixed_model):
    request, daily = inputs()
    daily["closing_qty"] = 50.
    result = calculation.calculate(request, daily).results[0]
    assert result.status.value == "NOT_NEEDED" and result.recommended_qty == 0
    assert result.scenarios == [] and result.insight_input is None


@pytest.mark.parametrize(("qty", "unmet"), [(1, 16), (7, 10)])
def test_custom_keeps_exact_input_without_order_unit_rounding(fixed_model, qty, unmet):
    request, daily = inputs(settings=[dict(productId=7, leadTimeDays=5, reviewCycleDays=14,
                                         orderUnitQty=5, unitCost=100)])
    result = calculation.custom_scenario(ScenarioRequest(resultId=1, calculationInput=request, qty=qty), daily)
    # 19일 × 하루 1개 수요에서 기초 2개와 직접 입력량만 충당한다.
    assert result.qty == qty and result.unmet_demand_qty == unmet
    assert result.expected_ending_stock == 0 and result.post_arrival_risk is None


def test_custom_zero_is_rejected_by_existing_contract(client):
    request, _ = inputs()
    response = client.post("/engine/scenarios", json=dict(resultId=1, calculationInput=request.to_json_dict(), qty=0))
    assert response.status_code == 422
