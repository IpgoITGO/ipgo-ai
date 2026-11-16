from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app import calculation, repository
from app.jobs import JobEnvelope, execute
from engine.model19 import Model19Error, Prediction, load_bundle
from schemas.engine import CalculateRequest, ScenarioRequest


def inputs(**changes):
    day = date(2026, 11, 10)
    payload = dict(runId=1, userId=1, warehouseId=1, uploadFileIds=[1], asOfDate=day, today=day,
                   productIds=[7], productCodes={7:"SYNTHETIC"},
                   policy=dict(defaultMethod="LGBM19", modelVersion="test-version", multiplier=1),
                   settings=[dict(productId=7, leadTimeDays=5, reviewCycleDays=14, orderUnitQty=1, unitCost=100)])
    payload.update(changes)
    daily = pd.DataFrame([dict(product_id=7, upload_file_id=1, stock_date=day-timedelta(days=i),
                               outbound_qty=1., closing_qty=2., is_valid_observation=True) for i in range(90)])
    return CalculateRequest.model_validate(payload), daily


@pytest.fixture
def bundle(monkeypatch):
    calls = []

    def predict(daily, barcode, origin):
        calls.append((daily.copy(), barcode, origin))
        return Prediction(19, 24, 30, 4, 28, False)

    stub = SimpleNamespace(version="test-version", predict=predict)
    monkeypatch.setattr("engine.model19.load_bundle", lambda *args: stub)
    return calls


def test_quantity_is_target_less_stock_and_pending_not_prearrival_loss(bundle):
    request, daily = inputs(plannedReceipts=[dict(productId=7, quantity=3, expectedOn="2026-11-12")])
    result = calculation.calculate(request, daily).results[0]
    assert result.recommended_qty == 23  # 28 - 2 - 3; 도착 전 손실 추가 차감 없음
    assert result.target_stock_qty == 28 and result.forecast_outbound_qty == 19
    assert result.pre_arrival_risk is result.risk_status is result.accuracy_grade is None
    assert result.accuracy_evaluation.status == "UNAVAILABLE"
    assert result.accuracy_evaluation.history == [] and result.insight_input is None
    assert result.method.value == "LGBM19" and result.model_version == "test-version"
    assert len(result.scenarios[0].daily_projection) == 19
    assert bundle[0][1] == "SYNTHETIC"


def test_http_worker_custom_share_model_and_order_units(client, monkeypatch, bundle):
    request, daily = inputs()
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(repository, "load_product_daily", lambda *args, **kwargs: daily)
    http = client.post("/engine/calculate", json=request.to_json_dict())
    assert http.status_code == 200
    job = JobEnvelope(contractVersion="1", jobId="synthetic", jobType="CALCULATE", payload=request.to_json_dict())
    assert execute(job).result == http.json()
    custom = calculation.custom_scenario(ScenarioRequest(resultId=1, calculationInput=request, qty=31), daily)
    assert custom.qty == 31 and len(custom.daily_projection) == 19


def test_unsupported_conditions_and_unknown_mapping_are_not_zero_success(bundle, monkeypatch):
    request, daily = inputs(settings=[
        dict(productId=7, leadTimeDays=6, reviewCycleDays=13, orderUnitQty=1, unitCost=1)])
    result = calculation.calculate(request, daily).results[0]
    assert result.status.value == "NOT_CALCULABLE" and result.recommended_qty is None

    def missing(*args):
        raise Model19Error("학습 상품 대응 정보가 없는 상품이에요")

    monkeypatch.setattr("engine.model19.load_bundle",
                        lambda *args: SimpleNamespace(version="test-version", predict=missing))
    request, daily = inputs()
    assert calculation.calculate(request, daily).results[0].recommended_qty is None


def test_missing_bundle_and_version_fail_closed(tmp_path):
    with pytest.raises(Model19Error):
        load_bundle(str(tmp_path), "missing")
    with pytest.raises(Model19Error):
        load_bundle("", "")


def test_gap_uses_19_day_projection_and_known_receipts(bundle):
    request, daily = inputs(today=date(2026, 11, 12),
                           plannedReceipts=[dict(productId=7, quantity=1, expectedOn="2026-11-11")])
    result = calculation.calculate(request, daily).results[0]
    assert result.start_stock_qty == 1 and result.start_stock_estimated
    assert result.horizon_days == 19 and result.forecast_outbound_qty == 19
    assert result.recommended_qty == 27
