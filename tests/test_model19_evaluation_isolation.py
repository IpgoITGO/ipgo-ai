"""#120: 같은 상품의 기존 A/B 표본이 모델 결과의 미산출 상태를 바꾸지 않는다."""
from datetime import date
from types import SimpleNamespace

import pytest

from app import calculation, repository
from app.jobs import JobEnvelope, execute
from engine import calculate as core
from engine.model19 import Prediction
from engine.risk_sampling import RiskSamplingPolicy, prepare_risk_context
from schemas.base import Method
from schemas.engine import ScenarioRequest
from tests.test_model19 import inputs


@pytest.fixture
def model_inputs(monkeypatch):
    request, daily = inputs()
    bundle = SimpleNamespace(version="test-version",
                             predict=lambda *args: Prediction(19, 24, 30, 4, 28, False))
    monkeypatch.setattr("engine.model19.load_bundle", lambda *args: bundle)
    return request, daily, bundle


def assert_unavailable(result):
    assert result.method == Method.LGBM19 and result.recommended_qty == 26
    assert result.risk_status is result.risk_basis is result.risk_sample_size is None
    assert result.pre_arrival_risk is result.accuracy_grade is None
    assert result.forecast_range_low is result.forecast_range_high is None
    assert result.accuracy_history == [] and result.insight_input is None
    assert all(s.post_arrival_risk is None for s in result.scenarios)


def test_core_ignores_existing_ab_samples_for_same_product(model_inputs):
    request, daily, bundle = model_inputs
    day = date(2026, 8, 1)
    delta = day - request.as_of_date
    daily = daily.assign(stock_date=daily.stock_date.map(lambda d: d + delta))
    request = request.model_copy(update={"as_of_date": day, "today": day})
    legacy = request.model_copy(update={"policy": request.policy.model_copy(
        update={"default_method": Method.B, "model_version": None})})
    policy = RiskSamplingPolicy("synthetic-AB", date(2026, 5, 1), date(2026, 7, 31), day,
                                (date(2026, 6, 15), date(2026, 7, 1)), 2)
    context = prepare_risk_context(legacy, daily, policy)
    assert context is not None
    assert any(s.product_id == 7 and s.method == Method.B for b in context.batches for s in b.samples)
    assert_unavailable(core.calculate(request, daily, risk_context=context, model_bundle=bundle).results[0])
    custom = core.custom_scenario(ScenarioRequest(resultId=1, calculationInput=request, qty=7), daily,
                                  risk_context=context, model_bundle=bundle)
    assert custom.qty == 7 and custom.post_arrival_risk is None


def test_http_worker_and_custom_do_not_load_ab_evaluation(client, monkeypatch, model_inputs):
    request, daily, _ = model_inputs

    def forbidden():
        pytest.fail("LGBM19 요청은 A/B 평가 정책·표본을 읽으면 안 된다")

    monkeypatch.setattr(calculation, "load_risk_policy", forbidden)
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(repository, "load_product_daily", lambda *args, **kwargs: daily)
    response = calculation.calculate(request, daily)
    result = response.results[0]
    assert_unavailable(result)
    evaluation = result.accuracy_evaluation
    assert evaluation.status == "UNAVAILABLE" and evaluation.reason == "EVALUATION_UNAVAILABLE"
    assert evaluation.history == [] and evaluation.policy_version is evaluation.policy_hash is None
    http = client.post("/engine/calculate", json=request.to_json_dict())
    assert http.status_code == 200 and http.json() == response.to_json_dict()
    job = JobEnvelope(contractVersion="1", jobId="synthetic-isolation", jobType="CALCULATE",
                      payload=request.to_json_dict())
    assert execute(job).result == http.json()
    custom = calculation.custom_scenario(ScenarioRequest(resultId=1, calculationInput=request, qty=7), daily)
    assert custom.qty == 7 and custom.post_arrival_risk is None
