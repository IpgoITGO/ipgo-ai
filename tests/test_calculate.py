"""DA-11 연결 검증. 숫자 기대값은 기존 사람이 작성한 DA-03/04/06 워크시트를 재사용한다."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app import repository
from app.jobs import JobEnvelope
from app.worker import process_message
from engine import ENGINE_VERSION
from engine.calculate import CalculationInputError, calculate, custom_scenario
from schemas.base import Method, ResultStatus, ScenarioType
from schemas.engine import CalculateRequest, ScenarioRequest

FIXTURES = Path(__file__).parent / "fixtures"


def table(name, heading):
    text = (FIXTURES / name).read_text()
    assert "작성 상태: 완료" in text
    section = text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    return [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in section.splitlines() if line.startswith("|")
    ][2:]


def daily_rows(product_id, as_of, demand, closing):
    return pd.DataFrame([
        {"product_id": product_id, "upload_file_id": 21, "stock_date": as_of - timedelta(days=k),
         "outbound_qty": demand, "closing_qty": closing, "is_valid_observation": True}
        for k in range(7)
    ])


def request_for(as_of, today, horizon, lead, unit=1, multiplier=1, receipts=()):
    return CalculateRequest(
        runId=1, userId=1, warehouseId=1, uploadFileIds=[21], asOfDate=as_of, today=today,
        productIds=[101], policy={"defaultMethod": "B", "multiplier": multiplier},
        settings=[{"productId": 101, "leadTimeDays": lead, "reviewCycleDays": horizon - lead,
                   "orderUnitQty": unit, "unitCost": 10}],
        plannedReceipts=[{"productId": 101, "expectedOn": d, "quantity": q} for d, q in receipts],
    )


def replenish_case(case):
    row = next(r for r in table("da04_replenish.md", "1. 입력") if r[0] == case)
    _, t, s, horizon, total, multiplier, closing, unit, _, planned = row
    as_of, today, horizon = date.fromisoformat(t), date.fromisoformat(s), int(horizon)
    receipts = [] if planned == "없음" else [
        (date.fromisoformat(d.strip()), int(q)) for d, q in (item.rsplit(":", 1) for item in planned.split(","))
    ]
    # L=3이면 워크시트의 도착 전 미충족(0/0/10/0)이 기존 simulate로 재현된다.
    request = request_for(as_of, today, horizon, 3, int(unit), float(multiplier), receipts)
    daily = daily_rows(101, as_of, float(total) / ((today - as_of).days + horizon), float(closing))
    expected = next(r[1:] for r in table("da04_replenish.md", "3. 기대값") if r[0] == case)
    return request, daily, expected, float(row[8])


@pytest.mark.parametrize("case", ["1", "2", "3", "4"])
def test_calculation_matches_da04_hand_calculations(case):
    request, daily, expected, unmet = replenish_case(case)
    original = daily.copy(deep=True)
    snapshot = request.model_dump()
    response = calculate(request, daily)
    [result] = response.results
    _, cover, stock, estimated, planned, need, qty = expected
    assert response.engine_version == ENGINE_VERSION
    assert result.forecast_outbound_qty == pytest.approx(float(cover))
    assert result.start_stock_qty == pytest.approx(float(stock))
    assert result.start_stock_estimated == (estimated == "O")
    assert result.planned_receipts_in_horizon == int(planned)
    assert result.pre_arrival_unmet_qty == pytest.approx(unmet)
    assert result.required_qty_before_rounding == pytest.approx(float(need))
    assert result.recommended_qty == int(qty)
    assert result.recommended_amount == int(qty) * request.settings[0].unit_cost
    assert result.status is (ResultStatus.OK if int(qty) else ResultStatus.NOT_NEEDED)
    assert [s.type for s in result.scenarios] == (
        [ScenarioType.P80, ScenarioType.P100, ScenarioType.P120] if int(qty) else []
    )
    for scenario in result.scenarios:
        assert scenario.qty % request.settings[0].order_unit_qty == 0
        assert len(scenario.daily_projection) == result.horizon_days
        assert scenario.daily_projection[0].date == request.today + timedelta(days=1)
        assert scenario.daily_projection[-1].date == request.today + timedelta(days=result.horizon_days)
        assert scenario.post_arrival_risk is None
    if not int(qty):
        assert result.expected_shortage_on is None
    elif result.expected_shortage_on is not None:
        assert request.today < result.expected_shortage_on <= request.today + timedelta(days=result.horizon_days)
    assert result.risk_status is None and result.pre_arrival_risk is None and result.risk_sample_size is None
    assert result.accuracy_grade is not None and result.cover_trial_count is None
    pd.testing.assert_frame_equal(daily, original)
    assert request.model_dump() == snapshot


@pytest.mark.parametrize("case", ["1", "2"])
def test_custom_scenario_matches_da06_hand_calculations(case):
    row = next(r for r in table("da06_simulate.md", "1. 입력") if r[0] == case)
    _, stock, demand, horizon, lead, qty, planned = row
    today = date(2026, 6, 15)
    receipts = [] if planned == "없음" else [
        (today + timedelta(days=int(day.strip().removeprefix("s+"))), int(q))
        for day, q in (item.split(":") for item in planned.split(","))
    ]
    request = request_for(today, today, int(horizon), int(lead), receipts=receipts)
    result = custom_scenario(
        ScenarioRequest(resultId=1, calculationInput=request, qty=int(qty)),
        daily_rows(101, today, float(demand), float(stock)),
    )
    expected = [r for r in table("da06_simulate.md", "3. 기대값 — 날짜별") if r[0] == case]
    assert result.type is ScenarioType.CUSTOM and result.qty == int(qty)
    assert [p.date for p in result.daily_projection] == [
        today + timedelta(days=int(row[1].removeprefix("s+"))) for row in expected
    ]
    assert [p.closing_qty for p in result.daily_projection] == pytest.approx([float(r[4]) for r in expected])
    _, depletion, ending, pre, post = next(
        r for r in table("da06_simulate.md", "4. 기대값 — 요약") if r[0] == case
    )
    assert result.expected_depletion_on == today + timedelta(days=int(depletion.removeprefix("s+")))
    assert result.expected_ending_stock == pytest.approx(float(ending))
    assert result.unmet_demand_qty == pytest.approx(float(pre) + float(post))


def forecast_case(barcode):
    rows = []
    for bc, start, end, demand, valid, _ in table("da03_forecast.md", "2. 입력 — 상품 일별 출고 (구간 단위)"):
        if bc != barcode:
            continue
        day, last = date.fromisoformat(start), date.fromisoformat(end)
        while day <= last:
            rows.append({"product_id": 101, "upload_file_id": 21, "stock_date": day,
                         "outbound_qty": float(demand), "closing_qty": 12, "is_valid_observation": valid == "O"})
            day += timedelta(days=1)
    t, horizon = table("da03_forecast.md", "1. 조건")[0]
    return request_for(date.fromisoformat(t), date.fromisoformat(t), int(horizon), 3), pd.DataFrame(rows)


@pytest.mark.parametrize("method", [Method.A, Method.B])
def test_selected_forecast_matches_da03_hand_calculations(method):
    request, daily = forecast_case("TEST-0003")
    request.policy.default_method = method
    [result] = calculate(request, daily).results
    heading = "5. 기대값 — 계산식 A" if method is Method.A else "4. 기대값 — 기준 B"
    expected = next(r for r in table("da03_forecast.md", heading) if r[0] == "TEST-0003")
    assert result.method is method
    assert result.forecast_outbound_qty == pytest.approx(float(expected[-2]))


def test_fallback_a_to_b_and_both_unavailable():
    request, daily = forecast_case("TEST-0004")
    request.policy.default_method = Method.A
    [result] = calculate(request, daily).results
    expected = next(r for r in table("da03_forecast.md", "4. 기대값 — 기준 B") if r[0] == "TEST-0004")
    assert result.method is Method.B and "A 사용 불가" in result.method_reason
    assert result.forecast_outbound_qty == pytest.approx(float(expected[-2]))

    request, daily = forecast_case("TEST-0005")
    [result] = calculate(request, daily).results
    assert result.status is ResultStatus.NOT_CALCULABLE
    assert result.recommended_qty is None and result.reason and not result.scenarios
    # 사유는 화면 · 인사이트 고정 문장에 그대로 보이므로 계산식 기호와 구간 기호를 쓰지 않는다 (#68).
    for symbol in ("A:", "B:", ";", "P_H", "C_90", "P_90", "유효 관측일"):
        assert symbol not in result.reason
    assert "판매한 날이" in result.reason and not result.reason.endswith(".")


def test_fallback_b_to_a_when_only_recent_observations_are_missing():
    request, daily = forecast_case("TEST-0003")
    daily.loc[daily.stock_date >= request.as_of_date - timedelta(days=27), "is_valid_observation"] = False
    [result] = calculate(request, daily).results
    assert result.method is Method.A and "B 사용 불가" in result.method_reason


def test_other_products_sources_and_future_data_do_not_change_result():
    request, daily, _, _ = replenish_case("1")
    expected = calculate(request, daily)
    combined = pd.concat([
        daily, daily.assign(product_id=202, outbound_qty=99999),
        daily.assign(upload_file_id=999, outbound_qty=99999),
        daily.assign(stock_date=lambda df: df.stock_date + timedelta(days=30), outbound_qty=99999),
    ], ignore_index=True)
    request.planned_receipts.append(
        request.planned_receipts[0].model_copy(update={"product_id": 202, "quantity": 99999})
    )
    assert calculate(request, combined) == expected


def test_all_products_have_independent_settings_stock_and_receipts():
    request, daily, _, _ = replenish_case("1")
    other_request, other_daily, _, _ = replenish_case("4")
    other_request.policy = request.policy.model_copy()
    expected = calculate(request, daily).results + [
        calculate(other_request, other_daily).results[0].model_copy(update={"product_id": 202})
    ]
    request.product_ids = None
    request.settings.append(other_request.settings[0].model_copy(update={"product_id": 202}))
    combined = pd.concat([daily, other_daily.assign(product_id=202)], ignore_index=True)
    assert calculate(request, combined).results == expected


@pytest.mark.parametrize("problem", ["empty", "missing_setting", "duplicate_day", "missing_as_of",
                                     "negative", "nan", "infinite", "missing_observation"])
def test_incomplete_product_is_not_reported_as_zero_order(problem):
    request, daily, _, _ = replenish_case("1")
    if problem == "empty":
        daily = pd.DataFrame()
    elif problem == "missing_setting":
        request.settings = []
    elif problem == "duplicate_day":
        daily = pd.concat([daily, daily.iloc[:1]], ignore_index=True)
    elif problem == "missing_as_of":
        daily = daily[daily.stock_date != request.as_of_date]
    elif problem == "missing_observation":
        daily["is_valid_observation"] = None
    else:
        daily["closing_qty"] = {"negative": -1, "nan": float("nan"), "infinite": float("inf")}[problem]
    [result] = calculate(request, daily).results
    assert result.status is ResultStatus.NOT_CALCULABLE and result.reason
    assert result.recommended_qty is None and result.recommended_amount is None
    assert result.scenarios == []


def test_missing_settings_are_reported_when_calculating_all_products():
    request, daily, _, _ = replenish_case("1")
    request.product_ids, request.settings = None, []
    [result] = calculate(request, daily).results
    assert result.product_id == 101 and result.status is ResultStatus.NOT_CALCULABLE


def test_rounded_duplicates_keep_recommended_scenario():
    request, daily, _, _ = replenish_case("1")
    request.settings[0].order_unit_qty = 1000
    [result] = calculate(request, daily).results
    low, recommended, high = result.scenarios
    assert low.qty == recommended.qty and low.is_duplicate and not recommended.is_duplicate
    assert not high.is_duplicate
    custom = custom_scenario(ScenarioRequest(resultId=1, calculationInput=request, qty=recommended.qty), daily)
    assert custom.is_duplicate and custom.daily_projection == recommended.daily_projection


@pytest.mark.parametrize("problem", ["date", "duplicate_settings", "duplicate_ids", "missing_columns"])
def test_invalid_calculation_input_is_explicit(problem):
    request, daily, _, _ = replenish_case("1")
    if problem == "date":
        request.today = request.as_of_date - timedelta(days=1)
    elif problem == "duplicate_settings":
        request.settings *= 2
    elif problem == "duplicate_ids":
        request.product_ids *= 2
    else:
        daily = daily.drop(columns="upload_file_id")
    with pytest.raises(CalculationInputError):
        calculate(request, daily)


def test_custom_rejects_multiple_products_and_unavailable_calculation():
    request, daily, _, _ = replenish_case("1")
    custom = ScenarioRequest(resultId=1, calculationInput=request, qty=20)
    request.product_ids = [101, 202]
    with pytest.raises(CalculationInputError, match="상품 1개"):
        custom_scenario(custom, daily)
    request.product_ids = [101]
    with pytest.raises(CalculationInputError, match="기록이 없어요"):
        custom_scenario(custom, pd.DataFrame())


def test_real_http_and_worker_execute_engine_not_mock(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    for case in ("1", "4"):
        request, daily, expected, _ = replenish_case(case)
        monkeypatch.setattr(repository, "load_product_daily", lambda *_, data=daily, **_kw: data)
        response = client.post("/engine/calculate", json=request.to_json_dict())
        assert response.status_code == 200
        body = response.json()
        assert body["engineVersion"] == ENGINE_VERSION
        assert body["results"][0]["recommendedQty"] == int(expected[-1])
        job = JobEnvelope(contractVersion="1", jobId=f"calculation-{case}", jobType="CALCULATE",
                          payload=request.to_json_dict())
        result = process_message(job.model_dump_json(by_alias=True))
        assert result.status == "SUCCEEDED" and result.result == body


def test_real_custom_http_and_invalid_snapshot(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    request, daily, _, _ = replenish_case("1")
    monkeypatch.setattr(repository, "load_product_daily", lambda *_, **_kw: daily)
    payload = ScenarioRequest(resultId=1, calculationInput=request, qty=20).to_json_dict()
    response = client.post("/engine/scenarios", json=payload)
    assert response.status_code == 200 and response.json()["type"] == "CUSTOM"
    assert response.json()["dailyProjection"][0]["date"] == (request.today + timedelta(days=1)).isoformat()
    payload["calculationInput"]["productIds"] = [101, 202]
    response = client.post("/engine/scenarios", json=payload)
    assert response.status_code == 422 and response.json()["code"] == "INVALID_INPUT"


def test_invalid_calculation_date_is_http_422_and_terminal_worker_failure(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    request, daily, _, _ = replenish_case("1")
    monkeypatch.setattr(repository, "load_product_daily", lambda *_, **_kw: daily)
    request.today = request.as_of_date - timedelta(days=1)
    response = client.post("/engine/calculate", json=request.to_json_dict())
    assert response.status_code == 422 and response.json()["code"] == "INVALID_INPUT"
    job = JobEnvelope(contractVersion="1", jobId="invalid-date", jobType="CALCULATE", payload=request.to_json_dict())
    result = process_message(job.model_dump_json(by_alias=True))
    assert result.status == "FAILED" and result.error.code == "INVALID_INPUT"


def test_no_new_order_shortage_date_matches_constant_daily_hand_calculation():
    # 45 on Oct 31 - two days * 5 = 35 on Nov 2; 35 / 5 = seven more days.
    request = request_for(date(2026, 10, 31), date(2026, 11, 2), horizon=12, lead=5)
    [result] = calculate(request, daily_rows(101, request.as_of_date, 5, 45)).results
    assert result.recommended_qty == 25
    assert result.expected_shortage_on == date(2026, 11, 9)


def test_existing_receipt_defers_shortage_in_same_coverage_window():
    # The known receipt contributes 10 units before depletion: (35 + 10) / 5 = nine days.
    request = request_for(date(2026, 10, 31), date(2026, 11, 2), horizon=12, lead=5,
                          receipts=[(date(2026, 11, 5), 10)])
    [result] = calculate(request, daily_rows(101, request.as_of_date, 5, 45)).results
    assert result.recommended_qty == 15
    assert result.expected_shortage_on == date(2026, 11, 11)
