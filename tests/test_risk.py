"""DA07. 기존 사람 DA06 정답과 항등/경계/단조 관계를 사용한다. 새 사람 정답표는 만들지 않는다."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta

import pandas as pd
import pytest

from engine.backtest import ErrorSample, ErrorSampleBatch, SamplePeriod, generate_error_samples
from engine.calculate import calculate, custom_scenario
from engine.risk import RiskContext, select_risk_samples
from engine.scenario import RiskInputError, simulate, stockout_risk
from schemas.base import Method, ResultStatus, RiskBasis, RiskStatus
from schemas.engine import NoDateReceiptInput, Period, PlannedReceiptInput, ScenarioRequest
from tests.test_backtest_samples import complete_case, hand_case  # noqa: F401
from tests.test_calculate import daily_rows, request_for, table

START, END = date(2026, 5, 1), date(2026, 7, 31)


def sample(ratio=1.0, *, product_id=101, method=Method.B, origin=START, horizon=10):
    """지어낸 입출력의 비율 변환 관계. min_samples=1/2 등 테스트값은 운영 정책이 아니다."""
    predicted = float(horizon)
    actual = predicted * ratio
    return ErrorSample(product_id, SamplePeriod.TUNE, method, origin, horizon, origin + timedelta(days=horizon),
                       predicted, actual, actual - predicted, ratio, None)


def batch(*samples, **overrides):
    return replace(ErrorSampleBatch(SamplePeriod.TUNE, START, END, END, samples, ()), **overrides)


def context(*samples, minimum=2, batches=None):
    return RiskContext((batch(*samples),) if batches is None else batches, START, END, minimum)


def select(ctx, **overrides):
    args = dict(product_id=101, method=Method.B, horizon_days=10, observed_through=END)
    args.update(overrides)
    return select_risk_samples(ctx, **args)


def replay(ratios=(1.0,), minimum=1, **overrides):
    args = dict(as_of_closing=12, daily_forecast=4, gap_days=0, horizon_days=10, order_qty=20,
                lead_time_days=3, gap_receipts={}, planned_receipts={})
    args.update(overrides)
    return stockout_risk(ratios, minimum, **args)


def test_sku_wins_at_minimum_and_pool_is_all_matching_skus():
    own = (sample(0.0), sample(1.0, origin=START + timedelta(days=1)))
    other = sample(100.0, product_id=202)
    chosen = select(context(*own, other))
    assert chosen.basis is RiskBasis.SKU and chosen.samples == own
    pooled = select(context(own[0], other))
    assert pooled.basis is RiskBasis.POOLED and pooled.samples == (own[0], other)
    too_few = select(context(other))
    assert too_few.basis is None and too_few.samples == (other,)
    assert not select(context()).samples and select(context()).basis is None


@pytest.mark.parametrize(("modified", "reason"), [
    (sample(method=Method.A), "OTHER_METHOD"),
    (sample(horizon=11), "OTHER_HORIZON"),
    (sample(origin=START - timedelta(days=1)), "OUTSIDE_TUNE"),
    (sample(origin=END), "OUTSIDE_TUNE"),
    (sample(origin=date(2026, 6, 15)), "NOT_OBSERVED_YET"),
    (replace(sample(), period=SamplePeriod.TEST), "NOT_TUNE"),
])
def test_selection_does_not_mix_method_length_time_or_phase(modified, reason):
    result = select(context(modified, minimum=1), observed_through=date(2026, 6, 1))
    assert result.basis is None and not result.samples
    assert [e.reason for e in result.excluded] == [reason]


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"period": SamplePeriod.TEST}, "NOT_TUNE"),
    ({"period_start": date(2025, 5, 1), "period_end": date(2025, 7, 31)}, "OTHER_TUNE_PERIOD"),
    ({"observed_through": START}, "NOT_OBSERVED_YET"),
])
def test_batch_provenance_is_checked(overrides, reason):
    result = select(context(batches=(batch(sample(), **overrides),), minimum=1))
    assert not result.samples and result.excluded[0].reason == reason


def test_window_end_at_cutoff_is_inclusive_and_overlaps_are_not_deduplicated():
    a = sample()
    b = sample(origin=START + timedelta(days=1))
    ctx = context(a, b)
    assert select(ctx, observed_through=a.target_end).samples == (a,)
    chosen = select(ctx, observed_through=b.target_end)
    assert chosen.samples == (a, b) and chosen.basis is RiskBasis.SKU


def test_zero_forecast_is_excluded_but_zero_actual_is_a_valid_ratio():
    zero = replace(sample(), forecast_qty=0.0, actual_qty=0.0, error_qty=0.0,
                   actual_to_forecast_ratio=None, ratio_unavailable_reason="ZERO_FORECAST")
    result = select(context(zero, sample(0.0, origin=START + timedelta(days=1))))
    assert result.ratios == (0.0,) and result.basis is None
    assert result.excluded[0].reason == "ZERO_FORECAST"


def test_nonfinite_ratio_from_valid_tiny_prediction_is_excluded():
    overflow = replace(sample(), forecast_qty=1e-300, actual_qty=1e300, error_qty=1e300,
                       actual_to_forecast_ratio=None, ratio_unavailable_reason="NONFINITE_RATIO")
    result = select(context(overflow))
    assert not result.samples and result.excluded[0].reason == "NONFINITE_RATIO"


@pytest.mark.parametrize("change", [
    {"actual_to_forecast_ratio": None}, {"actual_to_forecast_ratio": -1.0},
    {"actual_to_forecast_ratio": float("nan")}, {"actual_to_forecast_ratio": float("inf")},
    {"actual_to_forecast_ratio": True}, {"actual_to_forecast_ratio": 2.0},
    {"forecast_qty": -1.0}, {"actual_qty": float("inf")}, {"error_qty": 999},
    {"ratio_unavailable_reason": "ZERO_FORECAST"}, {"target_end": END}, {"product_id": -1},
])
def test_malformed_sample_is_error_not_insufficient_sample(change):
    with pytest.raises(RiskInputError):
        select(context(replace(sample(), **change)))


def test_duplicate_samples_fail_instead_of_reaching_minimum_twice():
    one = sample()
    with pytest.raises(RiskInputError, match="중복"):
        select(context(batches=(batch(one), batch(one))))


def test_selection_is_deterministic_and_preserves_inputs():
    a, b = sample(), sample(0.0, product_id=202)
    assert select(context(b, a)).samples == select(context(a, b)).samples
    with pytest.raises(FrozenInstanceError):
        select(context(a)).basis = RiskBasis.SKU
    assert a.actual_to_forecast_ratio == 1.0


@pytest.mark.parametrize("minimum", [0, -1, True, 1.5, None])
def test_threshold_is_explicit_positive_integer(minimum):
    with pytest.raises(RiskInputError):
        context(sample(), minimum=minimum)


def test_tune_period_cannot_be_replaced_by_test_or_arbitrary_calendar():
    with pytest.raises(RiskInputError):
        RiskContext((), date(2026, 8, 1), date(2026, 9, 30), 1)


@pytest.mark.parametrize("case", ["1", "2", "3"])
def test_identity_replay_matches_existing_human_da06_events(case):
    _, stock, demand, days, lead, qty, planned = next(
        r for r in table("da06_simulate.md", "1. 입력") if r[0] == case
    )
    receipts = {} if planned == "없음" else {
        int(day.strip().removeprefix("s+")): float(q)
        for day, q in (item.split(":") for item in planned.split(","))
    }
    _, _, _, pre, post = next(r for r in table("da06_simulate.md", "4. 기대값 — 요약") if r[0] == case)
    args = dict(as_of_closing=float(stock), daily_forecast=float(demand), horizon_days=int(days),
                lead_time_days=int(lead), order_qty=int(qty), planned_receipts=receipts)
    identity = replay(**args)
    assert identity.pre_arrival_risk == float(float(pre) > 0)
    assert identity.post_arrival_risk == float(float(post) > 0)
    # 비율0은 출고0이므로 사건도0. 위 사람이 정한 사건과 합쳐 빈도로 환산한다.
    mixture = replay((0.0, 1.0), **args)
    assert mixture.pre_arrival_risk == identity.pre_arrival_risk / 2
    assert mixture.post_arrival_risk == identity.post_arrival_risk / 2
    assert mixture.sample_size == 2


def test_zero_closing_is_not_unmet_demand():
    # DA06 경우3의 첫4일은 기말0에 정확히 닿지만 미충족은 없다.
    direct = simulate(10, [2.5] * 4, 0, 3, {})
    assert direct.depletion_day is not None
    estimate = replay(as_of_closing=10, daily_forecast=2.5, horizon_days=4, order_qty=0)
    assert estimate.pre_arrival_risk == estimate.post_arrival_risk == 0


def test_insufficient_samples_are_not_zero_risk():
    result = replay((), minimum=1)
    assert result.sample_size == 0 and result.pre_arrival_risk is result.post_arrival_risk is None
    assert replay((1.0,), minimum=2).pre_arrival_risk is None


def test_order_monotonicity_common_pre_risk_and_sample_order_invariance():
    ratios = (0.0, 0.5, 1.0, 2.0)
    estimates = [replay(ratios, order_qty=q) for q in (0, 20, 100)]
    assert len({e.pre_arrival_risk for e in estimates}) == 1
    assert [e.post_arrival_risk for e in estimates] == sorted(
        (e.post_arrival_risk for e in estimates), reverse=True)
    assert replay(tuple(reversed(ratios))) == replay(ratios)
    assert replay(ratios * 2).pre_arrival_risk == replay(ratios).pre_arrival_risk
    assert replay(ratios * 2).post_arrival_risk == replay(ratios).post_arrival_risk


def test_gap_prediction_is_scaled_and_stock_is_replayed_each_day():
    args = dict(as_of_closing=12, daily_forecast=4, gap_days=1, lead_time_days=2,
                horizon_days=2, order_qty=100)
    # 비율2: g일에8개를 출고하면 잔여4로 s+1의8개가 부족하다. 비율1이면 잔여8로 부족하지 않다.
    assert replay((1.0,), **args).pre_arrival_risk == 0
    assert replay((2.0,), **args).pre_arrival_risk == 1
    # 입고 전 판매 손실을 다음 날로 넘기지 않는다. g=2의 마지막 날 입고하면 잔여12다.
    restored = replay((1.0,), as_of_closing=0, daily_forecast=4, gap_days=2,
                      gap_receipts={2: 16}, lead_time_days=2, horizon_days=2, order_qty=0)
    assert restored.pre_arrival_risk == restored.post_arrival_risk == 0


def test_day_of_arrival_is_post_window_and_receipts_precede_shipments():
    args = dict(as_of_closing=0, daily_forecast=4, lead_time_days=1, horizon_days=1, order_qty=4)
    estimate = replay((1.0,), **args)
    assert estimate.pre_arrival_risk == estimate.post_arrival_risk == 0
    assert replay((2.0,), **args).post_arrival_risk == 1
    assert replay((2.0,), planned_receipts={1: 4}, **args).post_arrival_risk == 0


@pytest.mark.parametrize("ratio", [-1, float("nan"), float("inf"), True, None])
def test_invalid_ratio_is_error_even_below_minimum(ratio):
    with pytest.raises(RiskInputError):
        replay((ratio,), minimum=2)


@pytest.mark.parametrize("change", [
    {"gap_days": -1}, {"horizon_days": 0}, {"lead_time_days": 11}, {"order_qty": 1.5},
    {"as_of_closing": -1}, {"daily_forecast": float("inf")}, {"gap_receipts": {1: 4}},
    {"planned_receipts": {11: 4}}, {"planned_receipts": {1: -1}},
])
def test_invalid_scenario_is_not_reported_as_zero_risk(change):
    with pytest.raises(RiskInputError):
        replay(**change)


def test_nonfinite_reapplication_is_not_zero_risk():
    with pytest.raises(RiskInputError):
        replay((1e300,), daily_forecast=1e300)


def calculation_case(days=14, gap=0):
    as_of = date(2026, 6, 15)
    request = request_for(as_of, as_of + timedelta(days=gap), 10, 3)
    daily = daily_rows(101, as_of, 4, 12).iloc[:1]
    frame = pd.concat([daily.assign(stock_date=as_of - timedelta(days=k)) for k in range(days)], ignore_index=True)
    return request, frame


def test_calculate_context_fills_risk_and_facts_without_changing_baseline():
    request, frame = calculation_case()
    ctx = context(sample(0.0), sample(1.0, origin=START + timedelta(days=1)))
    original = frame.copy(deep=True)
    before = request.model_dump()
    baseline, = calculate(request, frame).results
    result, = calculate(request, frame, risk_context=ctx).results
    assert result.risk_status is RiskStatus.OK and result.risk_basis is RiskBasis.SKU
    assert result.risk_sample_size == 2 and result.insight_input is not None
    assert result.insight_input.risk_status is RiskStatus.OK
    ignore = {"risk_status", "risk_basis", "risk_sample_size", "pre_arrival_risk", "insight_input", "scenarios"}
    assert result.model_dump(exclude=ignore) == baseline.model_dump(exclude=ignore)
    for scenario, old in zip(result.scenarios, baseline.scenarios, strict=True):
        assert scenario.post_arrival_risk is not None
        assert scenario.model_dump(exclude={"post_arrival_risk"}) == old.model_dump(exclude={"post_arrival_risk"})
    assert request.model_dump() == before
    pd.testing.assert_frame_equal(frame, original)
    assert result.scenarios[0].post_arrival_risk >= result.scenarios[1].post_arrival_risk
    assert result.scenarios[1].post_arrival_risk >= result.scenarios[2].post_arrival_risk


@pytest.mark.parametrize("days", [7, 13])
def test_low_observation_count_suppresses_risk_and_uses_da13_range(days):
    request, frame = calculation_case(days)
    result, = calculate(request, frame, risk_context=context(sample(), minimum=1)).results
    assert result.risk_status is RiskStatus.LOW_GRADE and result.risk_sample_size == 1
    assert result.risk_basis is None and result.pre_arrival_risk is None
    assert all(s.post_arrival_risk is None for s in result.scenarios)
    assert result.accuracy_grade.value == "LOW"
    assert result.forecast_range_low is not None
    assert result.forecast_range_low <= result.forecast_range_high
    assert result.insight_input is not None  # LOW 범위가 준비되면 구조화 설명 입력을 만든다.


def test_pooled_and_real_insufficiency_are_distinct_from_missing_configuration():
    request, frame = calculation_case()
    pooled, = calculate(request, frame, risk_context=context(sample(product_id=202), minimum=1)).results
    assert pooled.risk_basis is RiskBasis.POOLED and pooled.risk_status is RiskStatus.OK
    small, = calculate(request, frame, risk_context=context(sample(product_id=202))).results
    assert small.risk_status is RiskStatus.INSUFFICIENT_SAMPLE and small.risk_sample_size == 1
    assert small.risk_basis is small.pre_arrival_risk is None
    assert small.insight_input is not None
    unset, = calculate(request, frame).results
    assert unset.risk_status is unset.risk_sample_size is unset.risk_basis is None
    assert unset.insight_input is None


def test_fallback_uses_actual_method_and_gap_requires_matching_forecast_length():
    request, frame = calculation_case()
    request.policy.default_method = Method.A
    ctx = context(sample(0.0), sample(100.0, method=Method.A), minimum=1)
    result, = calculate(request, frame, risk_context=ctx).results
    assert result.method is Method.B and result.method_fallback
    assert result.risk_status is RiskStatus.OK and result.pre_arrival_risk == 0
    assert all(s.post_arrival_risk == 0 for s in result.scenarios)
    request.today += timedelta(days=1)
    wrong_length, = calculate(request, frame, risk_context=ctx).results
    assert wrong_length.risk_status is RiskStatus.INSUFFICIENT_SAMPLE and wrong_length.risk_sample_size == 0
    right_length, = calculate(request, frame, risk_context=context(sample(0.0, horizon=11), minimum=1)).results
    assert right_length.risk_status is RiskStatus.OK


def test_custom_quantity_reuses_same_risk_context_and_handles_not_needed():
    request, frame = calculation_case()
    ctx = context(sample(0.0), sample(1.0, origin=START + timedelta(days=1)))
    result, = calculate(request, frame, risk_context=ctx).results
    p100 = result.scenarios[1]
    custom = custom_scenario(ScenarioRequest(result_id=1, calculation_input=request, qty=p100.qty),
                             frame, risk_context=ctx)
    assert custom.post_arrival_risk == p100.post_arrival_risk
    assert custom.expected_ending_stock == p100.expected_ending_stock
    frame["closing_qty"] = 10000
    not_needed, = calculate(request, frame, risk_context=ctx).results
    assert not_needed.status is ResultStatus.NOT_NEEDED and not not_needed.scenarios
    assert not_needed.insight_input is None
    assert custom_scenario(ScenarioRequest(result_id=1, calculation_input=request, qty=1),
                           frame, risk_context=ctx).post_arrival_risk == 0


def test_future_samples_are_not_leaked_to_a_historical_calculation():
    request, frame = calculation_case()
    future = sample(origin=request.as_of_date)
    result, = calculate(request, frame, risk_context=context(future, minimum=1)).results
    assert result.risk_status is RiskStatus.INSUFFICIENT_SAMPLE and result.risk_sample_size == 0
    assert result.pre_arrival_risk is None


def test_receipt_dates_are_shared_by_gap_and_risk_and_no_date_is_excluded():
    request, frame = calculation_case(gap=1)
    request.planned_receipts = [
        PlannedReceiptInput(product_id=101, expected_on=request.today, quantity=4),
        PlannedReceiptInput(product_id=101, expected_on=request.today + timedelta(days=2), quantity=4),
    ]
    ctx = context(sample(1.0, horizon=11), minimum=1)
    result, = calculate(request, frame, risk_context=ctx).results
    # 비율1의 g일/커버 기간 재생은 원래 시나리오의 미충족 사건과 일치해야 한다.
    assert result.pre_arrival_risk == float(result.pre_arrival_unmet_qty > 0)
    for scenario in result.scenarios:
        post_unmet = scenario.unmet_demand_qty - result.pre_arrival_unmet_qty
        assert scenario.post_arrival_risk == float(post_unmet > 0)
    request.no_date_receipts = [NoDateReceiptInput(product_id=101, quantity=100000)]
    extra, = calculate(request, frame, risk_context=ctx).results
    assert extra.scenarios == result.scenarios and extra.pre_arrival_risk == result.pre_arrival_risk
    assert extra.start_stock_qty == result.start_stock_qty and extra.recommended_qty == result.recommended_qty
    assert extra.no_date_receipt_qty == 100000


def test_custom_low_and_not_calculable_never_fabricate_risk():
    request, frame = calculation_case(days=7)
    ctx = context(sample(), minimum=1)
    custom = custom_scenario(ScenarioRequest(result_id=1, calculation_input=request, qty=20),
                             frame, risk_context=ctx)
    assert custom.post_arrival_risk is None
    result, = calculate(request, frame.iloc[:1], risk_context=ctx).results
    assert result.status is ResultStatus.NOT_CALCULABLE
    assert result.risk_status is result.risk_sample_size is result.pre_arrival_risk is None


def test_generated_da08_samples_flow_to_calculate_without_stubbing_the_engine(hand_case):  # noqa: F811
    frame, origin, horizon, _ = complete_case(hand_case)
    cutoff = origin + timedelta(days=horizon)
    generated = generate_error_samples(
        frame, origins=[origin], horizon_days=horizon, period=Period(start=START, end=END),
        period_name=SamplePeriod.TUNE, observed_through=cutoff, methods=[Method.B],
    )
    assert len(generated.samples) == 1 and generated.samples[0].actual_to_forecast_ratio == pytest.approx(1)
    frame = frame.assign(upload_file_id=21, closing_qty=12)
    request = request_for(cutoff, cutoff, horizon, 3)
    ctx = RiskContext((generated,), START, END, 1)
    result, = calculate(request, frame, risk_context=ctx).results
    assert result.risk_status is RiskStatus.OK and result.risk_sample_size == 1
    assert result.insight_input is not None
