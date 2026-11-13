"""HTTP/SQS 실제 표본 생성 연결. 정책과 자료는 합성이며 실측 최소 기준이 아니다."""
import hashlib
import json
import threading
from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import pytest

from app import calculation, repository, risk_policy
from app.errors import AppError
from app.jobs import JobEnvelope
from app.risk_policy import load_risk_policy
from app.worker import process_message, run_loop
from engine import calculate as core
from engine import risk_sampling
from engine.risk_sampling import RiskSamplingPolicy, prepare_risk_context
from schemas.base import Method, RiskStatus
from schemas.engine import ScenarioRequest
from tests.test_calculate import daily_rows, request_for

START, END, ASOF = date(2026, 5, 1), date(2026, 7, 31), date(2026, 8, 1)


def policy(**changes):
    return replace(RiskSamplingPolicy("synthetic-test-only", START, END, ASOF,
                                     (START, START + timedelta(days=7)), 2), **changes)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """테스트 전용 설정이다. 운영 기준/원본/키/배포를 변경하지 않는다."""
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setenv("IPGO_RISK_ENABLED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    obj = dict(version="synthetic-test-only", periodStart=str(START), periodEnd=str(END),
               availableOn=str(ASOF), origins=[str(START), str(START + timedelta(days=7))], minSamples=2)
    path = tmp_path / "risk-policy.json"
    def write(value=obj):
        content = json.dumps(value).encode()
        path.write_bytes(content)
        monkeypatch.setattr(risk_policy, "DEFAULT_POLICY_PATH", path)
        monkeypatch.setattr(risk_policy, "DEFAULT_POLICY_SHA256", hashlib.sha256(content).hexdigest())
    write()
    return obj, path, write


def case():
    request = request_for(ASOF, ASOF, horizon=10, lead=3)
    one = daily_rows(101, ASOF, 4, 12).iloc[:1]
    frame = pd.concat([one.assign(stock_date=ASOF - timedelta(days=k)) for k in range(121)], ignore_index=True)
    return request, frame


def message(request):
    return JobEnvelope(contractVersion="1", jobId="risk-synthetic", jobType="CALCULATE", payload=request.to_json_dict())


def test_explicit_disable_is_not_insufficient(monkeypatch):
    monkeypatch.setenv("IPGO_RISK_ENABLED", "false")
    assert load_risk_policy() is None


def test_default_is_approved_twenty_case_policy_with_real_decision_date(monkeypatch):
    monkeypatch.setenv("IPGO_RISK_ENABLED", "true")
    fixed = load_risk_policy()
    assert fixed.min_samples == 20 and fixed.available_on == date(2026, 11, 3)
    assert fixed.origins[0] == START and fixed.origins[-1] < END


@pytest.mark.parametrize("change", [
    {"minSamples": 0}, {"minSamples": True}, {"minSamples": 1.5}, {"version": ""},
    {"periodStart": "2026-08-01"}, {"availableOn": "2026-07-31"}, {"origins": []},
    {"origins": ["2026-05-01", "2026-05-01"]}, {"origins": ["2026-04-30"]},
    {"extra": "unknown"}, {"minSamples": None}, {"origins": "2026-05-01"},
])
def test_invalid_policy_does_not_invent_a_default(configured, change):
    obj, _, write = configured
    write(obj | change)
    with pytest.raises(AppError) as exc:
        load_risk_policy()
    assert exc.value.status == 503 and exc.value.code == "RISK_POLICY_INVALID"


@pytest.mark.parametrize("problem", ["invalid_digest", "missing_digest", "hash_changed", "missing_file",
                                     "oversized", "duplicate"])
def test_policy_is_content_pinned_and_bounded(configured, monkeypatch, problem):
    _, path, _ = configured
    if problem == "invalid_digest":
        monkeypatch.setattr(risk_policy, "DEFAULT_POLICY_SHA256", "broken")
    elif problem == "missing_digest":
        monkeypatch.setattr(risk_policy, "DEFAULT_POLICY_SHA256", None)
    elif problem == "hash_changed":
        path.write_text("{}")
    elif problem == "missing_file":
        path.unlink()
    else:
        content = b"x" * 16385 if problem == "oversized" else b'{"version":"first","version":"second"}'
        path.write_bytes(content)
        monkeypatch.setattr(risk_policy, "DEFAULT_POLICY_SHA256", hashlib.sha256(content).hexdigest())
    with pytest.raises(AppError, match="위험 계산 정책"):
        load_risk_policy()


def test_context_reuses_scope_cutoff_and_does_not_mutate_or_drop_pool_peers():
    request, frame = case()
    combined = pd.concat([frame, frame.assign(product_id=202),
                          frame.assign(product_id=303, stock_date=lambda df: df.stock_date + timedelta(days=500)),
                          frame.assign(product_id=404, upload_file_id=999)], ignore_index=True)
    before = combined.copy(deep=True)
    ctx = prepare_risk_context(request, combined, policy())
    assert {s.product_id for batch in ctx.batches for s in batch.samples} == {101, 202}
    assert all(s.target_end <= request.as_of_date for b in ctx.batches for s in b.samples)
    assert request.product_ids == [101]
    pd.testing.assert_frame_equal(combined, before)


def test_future_tuned_policy_is_not_applied_retroactively():
    request, frame = case()
    request.as_of_date = request.today = END
    assert prepare_risk_context(request, frame, policy()) is None


def test_history_only_product_cannot_change_pool_or_work_budget():
    request, frame = case()
    history_only = frame.assign(product_id=303, stock_date=lambda f: f.stock_date - timedelta(days=365))
    before = prepare_risk_context(request, frame, policy())
    after = prepare_risk_context(request, pd.concat([frame, history_only]), policy())
    assert before == after


def test_new_policy_can_use_older_inventory_without_reading_later_actuals():
    request, frame = case()
    request.as_of_date, request.today = END, ASOF
    context = prepare_risk_context(request, frame, policy())
    assert context is not None and all(s.target_end <= END for b in context.batches for s in b.samples)


def test_unique_g_plus_h_lengths_are_prepared_once_and_shared_by_a_b():
    request, frame = case()
    request.today += timedelta(days=1)
    request.product_ids = [101, 202, 303]
    request.settings.extend([
        request.settings[0].model_copy(update={"product_id": 202}),
        request.settings[0].model_copy(update={"product_id": 303, "review_cycle_days": 8}),
    ])
    ctx = prepare_risk_context(request, frame, policy())
    assert {s.horizon_days for b in ctx.batches for s in b.samples + b.skipped} == {10, 11, 12}
    assert len(ctx.batches) == 3
    assert {s.method for b in ctx.batches for s in b.samples + b.skipped} == {Method.A, Method.B}


def test_long_horizon_is_real_zero_available_samples_not_policy_missing():
    request, frame = case()
    request.settings[0].review_cycle_days = 100
    ctx = prepare_risk_context(request, frame, policy())
    assert ctx is not None and not ctx.batches
    result, = core.calculate(request, frame, risk_context=ctx).results
    assert result.risk_status is RiskStatus.INSUFFICIENT_SAMPLE and result.risk_sample_size == 0


def test_work_limit_preserves_calculation_without_inventing_insufficient_samples(monkeypatch):
    request, frame = case()
    monkeypatch.setattr(risk_sampling, "MAX_RISK_FORECASTS", 1)
    def no_plans(_daily, plans):
        assert plans == []
        return ()
    monkeypatch.setattr(risk_sampling, "generate_error_sample_batches", no_plans)
    ctx = prepare_risk_context(request, frame, policy())
    assert ctx.unavailable_horizons == (10,) and ctx.batches == ()
    result, = core.calculate(request, frame, risk_context=ctx).results
    baseline, = core.calculate(request, frame).results
    assert result == baseline
    assert result.risk_status is result.risk_sample_size is result.risk_basis is None


def test_capacity_limit_is_inclusive_and_complete_pool_is_unchanged(monkeypatch):
    request, frame = case()
    baseline = prepare_risk_context(request, frame, policy())
    # 1 SKU × 2 기준일 × A/B = 4회. 경계에서도 표본을 줄이지 않는다.
    monkeypatch.setattr(risk_sampling, "MAX_RISK_FORECASTS", 4)
    assert prepare_risk_context(request, frame, policy()) == baseline


def test_risk_lengths_take_priority_over_cover_history_and_are_order_independent(monkeypatch):
    request, frame = case()
    request.today += timedelta(days=1)
    # H=10, D=11의 각 완전한 pool 비용은 4회. D만 생성할 예산이다.
    monkeypatch.setattr(risk_sampling, "MAX_RISK_FORECASTS", 4)
    ctx = prepare_risk_context(request, frame, policy())
    assert ctx.unavailable_horizons == (10,)
    assert {s.horizon_days for b in ctx.batches for s in b.samples} == {11}
    monkeypatch.setattr(calculation, "load_risk_policy", policy)
    result, = calculation.calculate(request, frame).results
    assert result.risk_status is RiskStatus.OK
    assert result.accuracy_history == [] and result.cover_trial_count is None
    assert result.accuracy_evaluation.status == "UNAVAILABLE"
    assert result.accuracy_evaluation.reason == "EVALUATION_CAPACITY_EXCEEDED"
    assert prepare_risk_context(request, frame.iloc[::-1], policy()) == ctx


def test_only_the_unaffordable_product_length_is_left_unevaluated(monkeypatch):
    request, frame = case()
    request.product_ids = [101, 202]
    request.settings.append(request.settings[0].model_copy(update={"product_id": 202, "review_cycle_days": 8}))
    frame = pd.concat([frame, frame.assign(product_id=202)], ignore_index=True)
    # H=D=10/11 각각 2 SKU × 2 기준일 × A/B = 8회. 길이10만 들어간다.
    monkeypatch.setattr(risk_sampling, "MAX_RISK_FORECASTS", 8)
    ctx = prepare_risk_context(request, frame, policy())
    assert ctx.unavailable_horizons == (11,)
    accepted, deferred = core.calculate(request, frame, risk_context=ctx).results
    assert accepted.risk_status is RiskStatus.OK
    assert deferred.risk_status is deferred.risk_sample_size is deferred.risk_basis is None
    assert deferred.insight_input is None
    request.product_ids.reverse()
    request.settings.reverse()
    assert prepare_risk_context(request, frame.iloc[::-1], policy()) == ctx


def test_synthetic_185_product_issue111_reproduction_keeps_complete_risk_pools(monkeypatch):
    # 원본 계정/상품 값이 아닌 직접 만든 자료. 주간 기준일의 수를 손으로 합산한다.
    as_of = END
    request = request_for(as_of, as_of + timedelta(days=3), horizon=88, lead=5)
    request.product_ids = [101, 102, 103]
    request.settings.extend([
        request.settings[0].model_copy(update={"product_id": 102, "review_cycle_days": 11}),
        request.settings[0].model_copy(update={"product_id": 103, "review_cycle_days": 15}),
    ])
    one = pd.concat([daily_rows(101, as_of, 4, 12).iloc[:1].assign(stock_date=as_of-timedelta(days=k))
                     for k in range(122)], ignore_index=True)
    frame = pd.concat([one.assign(product_id=p) for p in range(101, 286)], ignore_index=True)
    fixed = policy(origins=tuple(START + timedelta(days=k) for k in range(0, 91, 7)), min_samples=20)
    # H=16/20/88과 D=19/23/91: 11+11+1+11+10+1=45, 185×2×45=16,650.
    # D는 모두 보존하고 H=20만 보류: 185×2×34=12,580회로 상한 이하.
    generated = []
    original = risk_sampling.generate_error_sample_batches
    def record(daily, plans):
        generated.extend(plans)
        return original(daily, plans)
    monkeypatch.setattr(risk_sampling, "generate_error_sample_batches", record)
    previous = request.model_copy(deep=True)
    previous.settings[-1].review_cycle_days = 83
    previous_ctx = prepare_risk_context(previous, frame, fixed)
    assert sum(len(p["product_ids"]) * 2 * len(p["origins"]) for p in generated) == 8_880
    assert previous_ctx.unavailable_horizons == ()
    generated.clear()
    ctx = prepare_risk_context(request, frame, fixed)
    assert ctx.unavailable_horizons == (20,)
    assert sum(len(p["product_ids"]) * 2 * len(p["origins"]) for p in generated) == 12_580
    assert {p["horizon_days"] for p in generated} == {16, 19, 23, 88, 91}
    assert all(len(p["product_ids"]) == 185 for p in generated)
    assert tuple(b for b in ctx.batches if b.samples[0].horizon_days != 23) == previous_ctx.batches
    response = core.calculate(request, frame, risk_context=ctx)
    baseline = core.calculate(request, frame)
    assert all(r.risk_status is RiskStatus.OK for r in response.results)
    assert all(r.risk_basis.value == "POOLED" for r in response.results)
    assert all(r.pre_arrival_risk is not None and r.risk_sample_size >= 20 for r in response.results)
    for actual, expected in zip(response.results, baseline.results, strict=True):
        assert actual.recommended_qty == expected.recommended_qty
        assert [s.daily_projection for s in actual.scenarios] == [s.daily_projection for s in expected.scenarios]


def test_http_sqs_and_custom_scenario_succeed_when_every_pool_exceeds_budget(client, configured, monkeypatch):
    request, frame = case()
    monkeypatch.setattr(risk_sampling, "MAX_RISK_FORECASTS", 1)
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: frame)
    http = client.post("/engine/calculate", json=request.to_json_dict())
    assert http.status_code == 200, http.text
    worker = process_message(message(request).model_dump_json(by_alias=True))
    assert worker.status == "SUCCEEDED" and worker.result == http.json()
    result = http.json()["results"][0]
    assert result["riskStatus"] is result["riskSampleSize"] is result["preArrivalRisk"] is None
    assert result["insightInput"] is None
    assert result["accuracyEvaluation"]["status"] == "UNAVAILABLE"
    assert result["accuracyEvaluation"]["reason"] == "EVALUATION_CAPACITY_EXCEEDED"
    baseline, = core.calculate(request, frame).results
    assert result["recommendedQty"] == baseline.recommended_qty
    assert result["scenarios"] == [s.to_json_dict() for s in baseline.scenarios]
    custom = ScenarioRequest(result_id=1, calculation_input=request, qty=result["scenarios"][1]["qty"])
    custom_http = client.post("/engine/scenarios", json=custom.to_json_dict())
    assert custom_http.status_code == 200
    assert custom_http.json()["postArrivalRisk"] is None


def test_http_and_sqs_share_real_generated_risk_and_preserve_baseline(client, configured, monkeypatch):
    request, frame = case()
    seen = []
    def load(warehouse, ids, *, user_id):
        seen.append((warehouse, ids, user_id))
        return frame
    monkeypatch.setattr(repository, "load_product_daily", load)
    baseline, = core.calculate(request, frame).results
    response = client.post("/engine/calculate", json=request.to_json_dict())
    assert response.status_code == 200, response.text
    body = response.json()
    result = body["results"][0]
    assert result["riskStatus"] == "OK" and result["riskBasis"] == "SKU" and result["riskSampleSize"] == 2
    assert result["insightInput"] is not None
    assert all(s["postArrivalRisk"] is not None for s in result["scenarios"])
    assert result["recommendedQty"] == baseline.recommended_qty
    assert [s["dailyProjection"] for s in result["scenarios"]] == [
        s.to_json_dict()["dailyProjection"] for s in baseline.scenarios]
    worker = process_message(message(request).model_dump_json(by_alias=True))
    assert worker.status == "SUCCEEDED" and worker.result == body
    assert seen == [(request.warehouse_id, request.upload_file_ids, request.user_id)] * 2


def test_pooled_and_insufficient_states_use_generated_cases(client, configured, monkeypatch):
    obj, _, write = configured
    request, frame = case()
    frame.loc[frame.stock_date == START + timedelta(days=1), "is_valid_observation"] = False
    peer = frame.assign(product_id=202, is_valid_observation=True)
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: pd.concat([frame, peer]))
    result = client.post("/engine/calculate", json=request.to_json_dict()).json()["results"][0]
    assert result["riskStatus"] == "OK" and result["riskBasis"] == "POOLED" and result["riskSampleSize"] == 3
    write(obj | {"minSamples": 4})
    result = client.post("/engine/calculate", json=request.to_json_dict()).json()["results"][0]
    assert result["riskStatus"] == "INSUFFICIENT_SAMPLE" and result["riskSampleSize"] == 3
    assert result["riskBasis"] is result["preArrivalRisk"] is None


def test_custom_http_reuses_same_policy_and_low_grade_is_suppressed(client, configured, monkeypatch):
    request, frame = case()
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: frame)
    result = client.post("/engine/calculate", json=request.to_json_dict()).json()["results"][0]
    custom = ScenarioRequest(result_id=1, calculation_input=request, qty=result["scenarios"][1]["qty"])
    response = client.post("/engine/scenarios", json=custom.to_json_dict())
    assert response.status_code == 200
    assert response.json()["postArrivalRisk"] == result["scenarios"][1]["postArrivalRisk"]
    frame = daily_rows(101, ASOF, 4, 12)
    low = client.post("/engine/calculate", json=request.to_json_dict()).json()["results"][0]
    assert low["riskStatus"] == "LOW_GRADE" and low["preArrivalRisk"] is None and low["insightInput"] is None
    assert client.post("/engine/scenarios", json=custom.to_json_dict()).json()["postArrivalRisk"] is None


def test_mock_skips_policy_and_db_while_missing_policy_keeps_real_null(client, monkeypatch):
    monkeypatch.setattr(risk_policy, "DEFAULT_POLICY_PATH", "nonexistent")
    monkeypatch.setattr(risk_policy, "DEFAULT_POLICY_SHA256", "broken")
    request, frame = case()
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: pytest.fail("mock DB 접근"))
    assert client.post("/engine/calculate", json=request.to_json_dict()).status_code == 200
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setenv("IPGO_RISK_ENABLED", "false")
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: frame)
    result = client.post("/engine/calculate", json=request.to_json_dict()).json()["results"][0]
    assert result["riskStatus"] is result["riskSampleSize"] is result["insightInput"] is None


def test_config_fault_is_http_503_and_keeps_worker_retry_semantics(client, configured, monkeypatch):
    _, path, _ = configured
    path.write_text("changed")
    request, frame = case()
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: frame)
    response = client.post("/engine/calculate", json=request.to_json_dict())
    assert response.status_code == 503 and response.json()["code"] == "RISK_POLICY_INVALID"
    with pytest.raises(AppError, match="위험 계산 정책"):
        process_message(message(request).model_dump_json(by_alias=True))


def test_invalid_sample_source_is_http_422_and_terminal_sqs_failure(client, configured, monkeypatch):
    request, frame = case()
    frame["product_id"] = "bad-id"
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: frame)
    response = client.post("/engine/calculate", json=request.to_json_dict())
    assert response.status_code == 422 and response.json()["code"] == "INVALID_INPUT"
    result = process_message(message(request).model_dump_json(by_alias=True))
    assert result.status == "FAILED" and result.error.code == "INVALID_INPUT"


def test_queue_loop_sends_real_risk_result_before_deleting_message(configured, monkeypatch):
    request, frame = case()
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: frame)
    stop = threading.Event()
    calls = []
    class Queue:
        received = False
        def receive_message(self, **_kw):
            if self.received:
                stop.set()
                return {}
            self.received = True
            return {"Messages": [{"Body": message(request).model_dump_json(by_alias=True), "ReceiptHandle": "fake"}]}
        def send_message(self, **kw):
            calls.append("send")
            payload = json.loads(kw["MessageBody"])
            assert payload["status"] == "SUCCEEDED" and payload["result"]["results"][0]["riskStatus"] == "OK"
        def delete_message(self, **_kw):
            calls.append("delete")
            stop.set()
    from app.config import get_settings
    run_loop(stop, get_settings(), Queue())
    assert calls == ["send", "delete"]

def test_default_policy_reaches_http_and_sqs_after_decision_without_llm(client, monkeypatch):
    import socket

    def no_network(*_args, **_kwargs):
        pytest.fail("위험 계산 중 외부 네트워크 호출")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request, frame = case()
    now = date(2026, 11, 3)
    extension = pd.concat([
        frame.iloc[:1].assign(stock_date=ASOF + timedelta(days=k))
        for k in range(1, (now - ASOF).days + 1)
    ], ignore_index=True)
    frame = pd.concat([frame, extension], ignore_index=True)
    frame = pd.concat([frame, frame.assign(product_id=202)], ignore_index=True)
    request.as_of_date = request.today = now
    monkeypatch.setattr(repository, "load_product_daily", lambda *_a, **_kw: frame)
    response = client.post("/engine/calculate", json=request.to_json_dict())
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["riskStatus"] == "OK" and result["riskBasis"] == "POOLED"
    assert result["riskSampleSize"] >= 20 and result["insightInput"] is not None
    worker = process_message(message(request).model_dump_json(by_alias=True))
    assert worker.status == "SUCCEEDED" and worker.result == response.json()


