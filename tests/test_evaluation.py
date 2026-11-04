"""합성 데이터의 항등식·불변성 검증. 사람이 승인한 숫자 fixture는 변경하지 않는다."""
import base64
import gzip
import hashlib
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from app.jobs import JobStatus, JobType, ResultEnvelope
from app.result_transport import MAX_RESULT_BYTES, encode_result
from app.worker import process_message
from engine.accuracy import empirical_range, grade
from engine.backtest import generate_error_samples, run_backtest
from engine.calculate import CalculationInputError
from engine.evaluation import choose_multiplier, errors
from schemas.base import AccuracyGrade
from schemas.engine import BacktestRequest, MultiplierMetric, Period, ProductSettings


def dataset(products=(1,)):
    # 동일 출고·입고, 일정 재고의 항등식. 실제 고객 데이터가 아니다.
    days = (date(2026, 10, 14) - date(2025, 1, 1)).days + 1
    return pd.DataFrame([dict(product_id=p, upload_file_id=1, stock_date=date(2025, 1, 1)+timedelta(days=k),
                              opening_qty=5, inbound_qty=1, outbound_qty=1, adjustment_qty=0,
                              closing_qty=5, is_valid_observation=True)
                         for p in products for k in range(days)])


def request(products=(1,)):
    return BacktestRequest(user_id=1, warehouse_id=1, upload_file_ids=[1],
        tune_period=Period(start=date(2026, 5, 1), end=date(2026, 7, 31)),
        test_period=Period(start=date(2026, 8, 1), end=date(2026, 10, 14)),
        settings=[ProductSettings(product_id=p, lead_time_days=2, review_cycle_days=8,
                                  order_unit_qty=1, unit_cost=1) for p in products])


@pytest.mark.parametrize("days,rate,expected", [
    (6,1,None),(7,None,AccuracyGrade.LOW),(13,1,AccuracyGrade.LOW),(14,1,AccuracyGrade.MEDIUM),
    (27,1,AccuracyGrade.MEDIUM),(28,None,AccuracyGrade.MEDIUM),
    (28,.89,AccuracyGrade.MEDIUM),(28,.9,AccuracyGrade.HIGH),
])
def test_grade_contract_boundaries(days, rate, expected):
    assert grade(days, rate) == expected


def test_constant_ratio_range_and_scale_equivariance():
    low, high = empirical_range(10, (1.,)*20)
    assert low == high == 10  # r=1이면 예측과 같은 항등식
    assert empirical_range(20, (.5,1.,2.)) == tuple(2*q for q in empirical_range(10, (.5,1.,2.)))


def test_zero_error_identity_and_no_denominator():
    batch = generate_error_samples(dataset(), origins=[date(2026,5,1)], horizon_days=10,
        period=request().tune_period, period_name="TUNE", observed_through=date(2026,7,31))
    assert errors(list(batch.samples)) == (0.,0.)  # 상수 출고의 동일 예측
    assert errors([]) == (None,None)


def test_target_miss_uses_best_rate_then_smallest_multiplier():
    metrics = [MultiplierMetric(multiplier=m, cover_success_rate=.5, excess_rate=0, meets_target=False)
               for m in (1.15,1.,1.05)]
    assert choose_multiplier(metrics).multiplier == min(m.multiplier for m in metrics)
    metrics[0].cover_success_rate = .8
    assert choose_multiplier(metrics) is metrics[0]


def test_test_changes_never_change_tune_selection_or_accuracy():
    daily = dataset()
    original = daily.copy(deep=True)
    first = run_backtest(request(), daily)
    changed = daily.copy()
    test = changed.stock_date >= date(2026,8,1)
    # 미래 실적만 바꾸며 재고 항등식은 유지한다.
    changed.loc[test, ["outbound_qty", "inbound_qty"]] = 2
    second = run_backtest(request(), changed)
    assert (first.selected_method, first.selected_multiplier, first.target_met) == (
        second.selected_method, second.selected_multiplier, second.target_met)
    assert first.multiplier_metrics == second.multiplier_metrics
    assert first.sku_accuracy == second.sku_accuracy
    assert first.method_errors[:2] == second.method_errors[:2]
    assert first.method_errors[2:] != second.method_errors[2:]
    assert all(h.origin_date.month in (5,6,7) for a in first.sku_accuracy for h in a.history)
    pd.testing.assert_frame_equal(daily, original)


def test_no_samples_are_null_and_not_false_success():
    daily = dataset().assign(is_valid_observation=False)
    response = run_backtest(request(), daily)
    assert response.selected_multiplier is None and not response.target_met
    assert all(m.mae is None and m.wape is None for m in response.method_errors)
    assert all(m.cover_success_rate is None and not m.meets_target for m in response.multiplier_metrics)
    assert response.scenario_reliability.ending_stock_mae is None
    assert all(b.actual_stockout_rate is None for b in response.scenario_reliability.risk_buckets)


def test_missing_settings_is_invalid_http_and_worker(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr("app.repository.load_product_daily", lambda *_a, **_k: dataset())
    req = request().model_copy(update={"settings": []})
    with pytest.raises(CalculationInputError):
        run_backtest(req, dataset())
    response = client.post("/engine/backtest", json=req.to_json_dict())
    assert response.status_code == 422 and response.json()["code"] == "INVALID_INPUT"
    job = dict(contractVersion="1",jobId="backtest",jobType="BACKTEST",payload=req.to_json_dict())
    assert process_message(json.dumps(job)).error.code == "INVALID_INPUT"


def test_backtest_http_sqs_equal_and_real_llm_absent(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("app.repository.load_product_daily", lambda *_a, **_k: dataset())
    response = client.post("/engine/backtest", json=request().to_json_dict())
    job = dict(contractVersion="1",jobId="backtest",jobType="BACKTEST",payload=request().to_json_dict())
    result = process_message(json.dumps(job))
    assert response.status_code == 200 and result.status == JobStatus.SUCCEEDED
    assert response.json() == result.result


def test_large_transport_is_lossless_bounded_and_deterministic():
    result = ResultEnvelope(job_id="fake", job_type=JobType.CLEAN, status=JobStatus.SUCCEEDED,
                            result={"fake": "가"*400_000})
    body = encode_result(result)
    assert len(body.encode()) < MAX_RESULT_BYTES and body == encode_result(result)
    outer = json.loads(body)
    raw = gzip.decompress(base64.b64decode(outer["payload"],validate=True))
    assert len(raw) == outer["uncompressedBytes"]
    assert hashlib.sha256(raw).hexdigest() == outer["sha256"]
    assert json.loads(raw) == result.to_json_dict()


def test_reliability_has_empirical_counts_without_claiming_confidence():
    # SKU별12건 미만이지만 동일 H·산식의 pooled는20건 이상이다.
    response = run_backtest(request((1,2,3)), dataset((1,2,3)))
    buckets = response.scenario_reliability.risk_buckets
    assert sum(b.cases for b in buckets) > 0
    for bucket in buckets:
        if bucket.cases < 20:
            assert bucket.actual_stockout_rate is None
        else:
            assert 0 <= bucket.actual_stockout_rate <= 1
    # 출고가 변하지 않는 항등식에서는 예상/관측 기말도 같다.
    assert response.scenario_reliability.ending_stock_mae == 0


def test_daily_future_duplicates_do_not_contaminate_prior_origin_samples():
    daily = dataset()
    origin = date(2026,5,1)
    args=dict(origins=[origin],horizon_days=10,period=request().tune_period,period_name="TUNE",
              observed_through=date(2026,7,31))
    first = generate_error_samples(daily, **args)
    later = daily[daily.stock_date == date(2026,9,1)]
    second = generate_error_samples(pd.concat([daily,later],ignore_index=True), **args)
    assert first == second


def test_oversized_decoded_result_is_rejected_before_publication(monkeypatch):
    import app.result_transport as transport
    monkeypatch.setattr(transport, "MAX_DECODED_BYTES", 100)
    result = ResultEnvelope(job_id="fake", job_type=JobType.CLEAN,status=JobStatus.SUCCEEDED,
                            result={"fake":"가"*400})
    with pytest.raises(ValueError):
        transport.encode_result(result)


def test_method_selection_uses_paired_cases_not_different_sku_populations():
    from dataclasses import replace

    from engine.evaluation import choose_method
    batch = generate_error_samples(dataset(),origins=[date(2026,5,1)],horizon_days=10,
        period=request().tune_period,period_name="TUNE",observed_through=date(2026,7,31))
    a, b = batch.samples
    assert a.method.value == "A" and b.method.value == "B"
    # A 정확 예측, B 과다 예측이면 동일 사례에서 A 우세.
    worse_b = replace(b, forecast_qty=b.forecast_qty*2,error_qty=-b.actual_qty)
    assert choose_method([a,worse_b])[0].value == "A"
    worse_a = replace(a, forecast_qty=a.forecast_qty*2,error_qty=-a.actual_qty)
    assert choose_method([worse_a,worse_b])[0].value == "B"
    assert choose_method([a,replace(worse_b, product_id=2)])[0].value == "B"


def test_multi_horizon_prefix_reuse_matches_independent_generation_with_future_poison():
    from engine.backtest import generate_error_sample_batches
    daily=dataset((1,2))
    plans=[dict(origins=[date(2026,5,1),date(2026,5,8)],horizon_days=h,
                period=request().tune_period,period_name="TUNE",observed_through=date(2026,7,31),product_ids=[1,2])
           for h in (10,34)]
    for source in [daily, daily.assign(outbound_qty=daily.outbound_qty.where(daily.stock_date<date(2026,7,1),-1))]:
        independent=tuple(generate_error_samples(source,**plan) for plan in plans)
        assert generate_error_sample_batches(source,plans) == independent


def test_repository_capacity_rejects_before_building_dataframe_and_rolls_back(monkeypatch):
    from types import SimpleNamespace

    from app import repository
    class TooMany:
        def __len__(self): return 150_001
        def __iter__(self): pytest.fail("상한 초과 자료를 DataFrame으로 만들면 안 됨")
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def execute(self,query,*_): self.query=query
        def fetchall(self):
            if "SELECT upload_file_id, user_id" in self.query:
                return [(1,1,1,"DONE")]
            assert self.query.count("DOUBLE PRECISION") == 5 and "LIMIT 150001" in self.query
            return TooMany()
    class Connection:
        read_only=False
        rolled_back=False
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def cursor(self): return Cursor()
        def rollback(self): self.rolled_back=True
    connection=Connection()
    monkeypatch.setattr(repository,"get_settings",lambda:SimpleNamespace(database_url="fake"))
    monkeypatch.setattr(repository.psycopg,"connect",lambda *_a,**_k:connection)
    with pytest.raises(CalculationInputError):
        repository.load_product_daily(1,[1],user_id=1)
    assert connection.read_only and connection.rolled_back


def test_partial_settings_can_use_other_source_skus_as_risk_pool(client, monkeypatch):
    # 설정은 평가 대상만 지정한다. 다른 원본 SKU는 동일 조건의 위험 pool로 남는다.
    req, daily = request((1,)), dataset((1,2))
    response = run_backtest(req, daily)
    assert {a.product_id for a in response.sku_accuracy} == {1}
    assert {a.product_id for a in response.actual_inbound_comparison} == {1}
    assert sum(b.cases for b in response.scenario_reliability.risk_buckets) > 0
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr("app.repository.load_product_daily", lambda *_a, **_k: daily)
    http = client.post("/engine/backtest", json=req.to_json_dict())
    job = dict(contractVersion="1", jobId="partial", jobType="BACKTEST", payload=req.to_json_dict())
    assert http.status_code == 200
    assert http.json() == process_message(json.dumps(job)).result == response.to_json_dict()


def test_large_stock_does_not_hide_a_broken_balance_identity():
    from engine.evaluation import case_from_sample, index_daily
    daily = dataset()
    daily[['opening_qty','closing_qty']] *= 10**12
    batch = generate_error_samples(daily, origins=[date(2026,5,1)], horizon_days=10,
        period=request().tune_period, period_name='TUNE', observed_through=date(2026,7,31))
    sample = batch.samples[0]
    assert case_from_sample(sample, index_daily(daily)) is not None
    broken = daily.copy()
    broken.loc[broken.stock_date==date(2026,5,2),'opening_qty'] += 1
    assert case_from_sample(sample, index_daily(broken)) is None
