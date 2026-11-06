"""#93: 같은 CALCULATE의 TUNE 과거 재생 이력. 합성 자료만 사용한다."""

from datetime import date, timedelta

import pandas as pd
import pytest

from app import calculation, repository
from app.jobs import JobEnvelope, execute
from app.risk_policy import load_risk_policy
from app.worker import process_message
from engine.calculate import calculate as core_calculate
from engine.risk_sampling import prepare_risk_context
from schemas.engine import CalculateRequest


def fixture():
    day = date(2026, 11, 3)
    request = CalculateRequest(
        runId=1,
        userId=1,
        warehouseId=1,
        uploadFileIds=[21],
        asOfDate=day,
        today=day,
        productIds=[101],
        policy={"defaultMethod": "B", "multiplier": 1},
        settings=[dict(productId=101, leadTimeDays=3, reviewCycleDays=7, orderUnitQty=1, unitCost=10)],
    )
    start = date(2025, 1, 1)
    frame = pd.DataFrame(
        [
            dict(
                product_id=101,
                upload_file_id=21,
                stock_date=start + timedelta(days=k),
                opening_qty=5.0,
                inbound_qty=1.25,
                outbound_qty=1.25,
                adjustment_qty=0.0,
                closing_qty=5.0,
                is_valid_observation=True,
            )
            for k in range((day - start).days + 1)
        ]
    )
    return request, frame


@pytest.fixture(autouse=True)
def real(monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setenv("IPGO_RISK_ENABLED", "true")


def test_same_calculation_metrics_history_and_reproducible_digest():
    request, frame = fixture()
    baseline = core_calculate(request, frame, risk_context=prepare_risk_context(request, frame, load_risk_policy()))
    response = calculation.calculate(request, frame)
    (result,) = response.results
    assert response.to_json_dict()["results"][0].keys() == baseline.to_json_dict()["results"][0].keys()
    assert result.model_dump(exclude={"accuracy_evaluation"}) == baseline.results[0].model_dump(
        exclude={"accuracy_evaluation"}
    )
    e = result.accuracy_evaluation
    assert e.status == "AVAILABLE" and e.horizon_days == 10
    assert len(e.history) == result.cover_trial_count == 12
    assert sum(h.covered for h in e.history) == result.cover_success_count
    # 일정 출고 1.25개×10일의 손계산. 정수 반올림 전 원본을 유지한다.
    assert all(h.forecast_qty == pytest.approx(12.5) and h.actual_qty == 12.5 for h in e.history)
    assert result.forecast_error_rate == 0
    assert e == calculation.calculate(request, frame.iloc[::-1]).results[0].accuracy_evaluation
    assert len(e.data_hash) == len(e.policy_hash) == 64
    # #74의 유한 수량 fixture에서 기록한 digest. 오류 표식 추가가 기존 hash를 바꾸면 안 된다.
    assert e.data_hash == "76c390a9a04cf80d7b2b593db3cfc7e93496b1827e325f02f802e0d9f313e1ac"


def test_future_rows_do_not_enter_history_or_digest():
    request, frame = fixture()
    future = frame.iloc[-1:].assign(stock_date=date(2027, 1, 1), inbound_qty=999.0, outbound_qty=999.0)
    left = calculation.calculate(request, frame)
    right = calculation.calculate(request, pd.concat([frame, future], ignore_index=True))
    assert left.results[0].accuracy_evaluation == right.results[0].accuracy_evaluation


def test_empty_and_disabled_do_not_create_success(monkeypatch):
    request, frame = fixture()
    no_tune = frame[frame.stock_date > date(2026, 7, 31)]
    e = calculation.calculate(request, no_tune).results[0].accuracy_evaluation
    assert e.status == "EMPTY" and e.history == []
    monkeypatch.setenv("IPGO_RISK_ENABLED", "false")
    result = calculation.calculate(request, frame).results[0]
    assert result.accuracy_evaluation.status == "UNAVAILABLE"
    assert result.accuracy_evaluation.history == [] and result.cover_trial_count is None


def test_worker_matches_application(monkeypatch):
    request, frame = fixture()
    monkeypatch.setattr(repository, "load_product_daily", lambda *a, **kw: frame)
    envelope = execute(
        JobEnvelope.model_validate(
            dict(contractVersion="1", jobId="synthetic93", jobType="CALCULATE", payload=request.to_json_dict())
        )
    )
    assert envelope.result == calculation.calculate(request, frame).to_json_dict()


def test_real_http_and_worker_history_match_without_llm(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request, frame = fixture()
    monkeypatch.setattr(repository, "load_product_daily", lambda *a, **kw: frame)
    http = client.post("/engine/calculate", json=request.to_json_dict())
    assert http.status_code == 200
    worker = execute(
        JobEnvelope.model_validate(
            dict(contractVersion="1", jobId="synthetic-http93", jobType="CALCULATE", payload=request.to_json_dict())
        )
    )
    assert http.json() == worker.result
    assert len(http.json()["results"][0]["accuracyEvaluation"]["history"]) == 12
    assert "accuracyHistory" not in http.json()["results"][0]


def test_empty_dataframe_without_columns_remains_a_not_calculable_result():
    request, _ = fixture()
    response = calculation.calculate(request, pd.DataFrame())
    result, = response.results
    assert result.status.value == "NOT_CALCULABLE"
    assert result.accuracy_evaluation.status == "UNAVAILABLE"
    assert result.accuracy_evaluation.history == []


@pytest.mark.parametrize("day,risk_enabled", [
    pytest.param(date(2026, 6, 15), "true", id="before-policy"),
    pytest.param(date(2026, 11, 3), "false", id="disabled-policy"),
])
@pytest.mark.parametrize("invalid_quantity", [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(None, id="missing"),
    pytest.param("not-a-number", id="invalid-text"),
])
def test_digest_keeps_product_failures_isolated_in_http_and_worker(
    client, monkeypatch, day, risk_enabled, invalid_quantity,
):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setenv("IPGO_RISK_ENABLED", risk_enabled)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request, frame = fixture()
    request.as_of_date = request.today = day
    request.product_ids = [101, 102]
    request.settings.append(request.settings[0].model_copy(update={"product_id": 102}))
    problem = frame.assign(product_id=102).astype({"closing_qty": object})
    problem.loc[problem.stock_date == day, "closing_qty"] = invalid_quantity
    daily = pd.concat([frame, problem], ignore_index=True)
    monkeypatch.setattr(repository, "load_product_daily", lambda *a, **kw: daily)
    baseline = core_calculate(request, daily).to_json_dict()

    http = client.post("/engine/calculate", json=request.to_json_dict())
    assert http.status_code == 200
    worker = process_message(JobEnvelope(
        contractVersion="1", jobId="synthetic-digest-regression", jobType="CALCULATE",
        payload=request.to_json_dict(),
    ).model_dump_json(by_alias=True))
    assert worker.status.value == "SUCCEEDED"
    assert worker.error is None and worker.result == http.json()
    results = http.json()["results"]
    assert [(r["productId"], r["status"]) for r in results] == [(101, "OK"), (102, "NOT_CALCULABLE")]
    assert "수불부를 확인해 주세요" in results[1]["reason"]
    for result, original in zip(results, baseline["results"], strict=True):
        evaluation = result.pop("accuracyEvaluation")
        original.pop("accuracyEvaluation")
        assert result == original
        assert evaluation["status"] == "UNAVAILABLE" and evaluation["history"] == []
        assert len(evaluation["dataHash"]) == 64

    # 입력 순서와 무관하게 같은 표식을 기록하고, 비유한 JSON 숫자를 응답에 넣지 않는다.
    replay = calculation.calculate(request, daily.iloc[::-1]).to_json_dict()
    assert replay == worker.result
