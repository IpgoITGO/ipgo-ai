"""#141: 리드타임 + 입고 주기가 366일을 넘는 상품은 그 상품만 계산 불가로 돌려준다. 합성 입력만 쓴다."""
from datetime import date

import pandas as pd
import pytest

from app import calculation, jobs, repository
from app.execution import HORIZON_TOO_LONG_REASON, validate_calculation_capacity
from engine.calculate import CalculationInputError
from schemas.engine import CalculateRequest, ScenarioRequest

# 5월 1~31일 매일 출고 1개, 마감 재고 9개. 기준일 = 오늘 = 5/31, 방식 B.
# B 하루 예상 출고 = 28일 출고 28개 ÷ 유효일 28일 = 1개.
# 계산 기간 H일 필요량 = 1 × H - 시작 재고 9 → H=66이면 57개, H=366이면 357개.


def _daily(products):
    return pd.DataFrame([{
        "product_id": product, "upload_file_id": 1, "stock_date": date(2026, 5, day),
        "outbound_qty": 1, "closing_qty": 9, "is_valid_observation": True,
    } for product in products for day in range(1, 32)])


def _request(horizons, product_ids=None):
    """horizons: {상품 ID: (리드타임, 입고 주기)}."""
    return CalculateRequest.model_validate({
        "runId": 1, "userId": 1, "warehouseId": 1, "uploadFileIds": [1],
        "asOfDate": "2026-05-31", "today": "2026-05-31", "policy": {"defaultMethod": "B"},
        "productIds": product_ids,
        "settings": [{"productId": product, "leadTimeDays": lead, "reviewCycleDays": review,
                      "orderUnitQty": 1, "unitCost": 10} for product, (lead, review) in horizons.items()],
        "noDateReceipts": [{"productId": 2, "quantity": 4}],
    })


def _execute(request):
    return jobs.execute(jobs.JobEnvelope(
        contract_version="1", job_id="horizon-141", job_type=jobs.JobType.CALCULATE,
        payload=request.to_json_dict(),
    ))


def test_only_oversized_product_is_not_calculable_in_sqs_and_http(client, monkeypatch):
    # QA 계정처럼 23 + 555 = 578일 상품 하나가 섞여도 나머지 상품은 계산한다.
    request = _request({1: (5, 61), 2: (23, 555), 3: (5, 61)})
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: _daily([1, 2, 3]))
    validate_calculation_capacity(request)
    worker = _execute(request)
    assert worker.status == jobs.JobStatus.SUCCEEDED
    rows = worker.result["results"]
    assert [row["productId"] for row in rows] == [1, 2, 3]
    for row in (rows[0], rows[2]):
        assert row["status"] == "OK" and row["recommendedQty"] == 57 and len(row["scenarios"]) == 3
    skipped = rows[1]
    assert skipped["status"] == "NOT_CALCULABLE"
    assert skipped["reason"] == HORIZON_TOO_LONG_REASON == "리드타임과 입고 주기를 합쳐 366일 이하로 넣어 주세요"
    assert skipped["recommendedQty"] is None and skipped["horizonDays"] is None and skipped["scenarios"] == []
    assert skipped["noDateReceiptQty"] == 4
    assert skipped["accuracyEvaluation"]["status"] == "UNAVAILABLE"

    monkeypatch.setenv("ENGINE_MODE", "real")
    http = client.post("/engine/calculate", json=request.to_json_dict())
    assert http.status_code == 200, http.text
    assert http.json() == worker.result


def test_oversized_product_matches_engine_not_calculable_shape(monkeypatch):
    # 엔진이 만드는 다른 계산 불가 결과(입고 조건 없음)와 같은 필드만 채운다.
    daily = _daily([1, 2])
    mixed = calculation.calculate(_request({1: (5, 61), 2: (23, 555)}), daily).results[1]
    missing = calculation.calculate(_request({1: (5, 61)}), daily).results[1]
    assert mixed.product_id == missing.product_id == 2
    ignored = {"reason", "valid_observation_days", "insight_input"}
    assert mixed.model_dump(exclude=ignored) == missing.model_dump(exclude=ignored)
    assert mixed.insight_input == missing.insight_input


def test_oversized_product_does_not_change_other_results():
    daily = _daily([1, 2, 3])
    alone = calculation.calculate(_request({1: (5, 61), 3: (5, 61)}, [1, 3]), daily).results
    mixed = calculation.calculate(_request({1: (5, 61), 2: (23, 555), 3: (5, 61)}), daily).results
    assert [r.model_dump() for r in alone] == [r.model_dump() for r in (mixed[0], mixed[2])]


@pytest.mark.parametrize(("review", "status", "qty"), [
    (365, "OK", 357),  # 1 + 365 = 366일: 366 × 1 - 9 = 357개
    (366, "NOT_CALCULABLE", None),  # 1 + 366 = 367일
])
def test_horizon_boundary(review, status, qty):
    request = _request({2: (1, review)})
    validate_calculation_capacity(request)
    result, = calculation.calculate(request, _daily([2])).results
    assert result.status.value == status and result.recommended_qty == qty
    if status == "NOT_CALCULABLE":
        assert result.reason == HORIZON_TOO_LONG_REASON


def test_only_oversized_products_return_all_not_calculable():
    results = calculation.calculate(_request({2: (23, 555), 4: (1, 366)}), _daily([2, 4])).results
    assert [(r.product_id, r.status.value, r.reason) for r in results] == [
        (2, "NOT_CALCULABLE", HORIZON_TOO_LONG_REASON), (4, "NOT_CALCULABLE", HORIZON_TOO_LONG_REASON)]


def test_whole_request_limits_still_fail_and_skip_oversized_points():
    # 1,000개 × 3 × 20일 = 60,000: 허용. 578일 상품은 시나리오가 없어 합산하지 않는다.
    horizons = {n: (5, 15) for n in range(1000)} | {1000: (23, 555)}
    validate_calculation_capacity(_request(horizons))
    # 1,001개 × 3 × 20일 = 60,060: 요청 전체 실패는 그대로다.
    with pytest.raises(CalculationInputError, match="시나리오"):
        validate_calculation_capacity(_request(horizons | {1001: (5, 15)}))
    gap = _request({1: (5, 61), 2: (23, 555)})
    gap.as_of_date = date(2024, 5, 30)  # 2026-05-31까지 731일
    with pytest.raises(CalculationInputError, match="730일"):
        validate_calculation_capacity(gap)


def test_custom_scenario_follows_same_rule(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: _daily([1, 2]))
    horizons = {1: (5, 61), 2: (23, 555)}
    # 정상 상품만 고르면 같은 입력에 578일 상품 설정이 있어도 계산한다. 직접 수량 57개.
    ok = ScenarioRequest(result_id=1, calculation_input=_request(horizons, [1]), qty=57)
    response = client.post("/engine/scenarios", json=ok.to_json_dict())
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "CUSTOM" and response.json()["qty"] == 57
    # 그 상품 하나만 고르면 기존 계산 불가와 같이 이유를 담은 입력 오류다.
    bad = ScenarioRequest(result_id=1, calculation_input=_request(horizons, [2]), qty=10)
    response = client.post("/engine/scenarios", json=bad.to_json_dict())
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"
    assert response.json()["message"] == HORIZON_TOO_LONG_REASON
