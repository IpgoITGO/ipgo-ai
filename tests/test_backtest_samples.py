"""DA08 최소 표본. 수치 정답은 기존 DA03 손계산 표를 재사용하고 새 정답 fixture는 만들지 않는다."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from engine import backtest
from engine.backtest import BacktestInputError, SamplePeriod, SkipReason, generate_error_samples
from engine.forecast import ForecastB
from schemas.base import Method
from schemas.engine import Period
from tests.test_forecast import WORKSHEET, _table
from tests.test_forecast import daily as forecast_fixture


@pytest.fixture
def hand_case():
    text = WORKSHEET.read_text()
    assert "작성 상태: 완료" in text
    [[origin, days]] = _table(text, "1. 조건")
    origin, days = date.fromisoformat(origin), int(days)
    return text, origin, days, forecast_fixture.__wrapped__(text)


def complete_case(hand_case, method=Method.B, barcode="TEST-0003"):
    """사람 기대 예측량을 H일에 균등 배치해 항등 오차(실적=예측)인 합성 평가 입력을 만든다."""
    text, origin, horizon, by_barcode = hand_case
    section = "4. 기대값 — 기준 B" if method == Method.B else "5. 기대값 — 계산식 A"
    row = next(r for r in _table(text, section) if r[0] == barcode and (method == Method.B or r[1] == "없음"))
    expected = float(row[4] if method == Method.B else row[6])
    history = by_barcode[barcode].loc[lambda df: df.stock_date <= origin].copy()
    history["product_id"] = 101
    future = pd.DataFrame([{"product_id": 101, "stock_date": origin + timedelta(days=k),
                            "outbound_qty": expected / horizon, "is_valid_observation": True}
                           for k in range(1, horizon + 1)])
    return pd.concat([history, future], ignore_index=True), origin, horizon, expected


def evaluate(frame, origin, horizon, **overrides):
    args = dict(origins=[origin], horizon_days=horizon,
                period=Period(start=origin, end=origin + timedelta(days=horizon)),
                period_name=SamplePeriod.TUNE, observed_through=origin + timedelta(days=horizon),
                product_ids=[101], methods=[Method.B])
    args.update(overrides)
    return generate_error_samples(frame, **args)


@pytest.mark.parametrize(("method", "barcode"), [(Method.A, "TEST-0003"), (Method.B, "TEST-0003"),
                                                 (Method.B, "TEST-0004")])
def test_samples_reuse_human_forecast_numbers_and_identity_error(hand_case, method, barcode):
    frame, origin, horizon, expected = complete_case(hand_case, method, barcode)
    before = frame.copy(deep=True)
    batch = evaluate(frame, origin, horizon, methods=[method])
    sample, = batch.samples
    assert not batch.skipped
    assert sample.forecast_qty == pytest.approx(expected)  # 기존 사람이 계산한40/45/50
    assert sample.actual_qty == pytest.approx(expected)
    assert sample.error_qty == pytest.approx(0) and sample.absolute_error == pytest.approx(0)
    assert sample.actual_to_forecast_ratio == pytest.approx(1)
    assert sample.ratio_unavailable_reason is None
    assert (sample.origin_date, sample.target_end, sample.horizon_days) == (
        origin, origin + timedelta(days=horizon), horizon)
    assert sample.product_id == 101 and sample.method == method and sample.period == SamplePeriod.TUNE
    assert (batch.period_start, batch.period_end, batch.observed_through) == (
        origin, origin + timedelta(days=horizon), origin + timedelta(days=horizon))
    pd.testing.assert_frame_equal(frame, before)
    with pytest.raises(FrozenInstanceError):
        sample.forecast_qty = -1


def test_forecast_inputs_are_cut_at_each_origin_and_keep_last_year(hand_case, monkeypatch):
    frame, origin, horizon, _ = complete_case(hand_case)
    seen = []
    original = backtest.forecast_a

    def guarded(history, cutoff, days):
        assert (history.stock_date <= cutoff).all()
        assert history.stock_date.min().year < cutoff.year  # TUNE 시작일 이전의 작년 자료도 필요
        seen.append(cutoff)
        return original(history, cutoff, days)

    monkeypatch.setattr(backtest, "forecast_a", guarded)
    first = origin - timedelta(days=1)
    last = origin + timedelta(days=1)
    batch = evaluate(frame, origin, horizon, methods=[Method.A], origins=[last, first, origin],
                     period=Period(start=first, end=last + timedelta(days=horizon)),
                     observed_through=last + timedelta(days=horizon))
    assert seen == [first, origin, last]
    assert {s.origin_date for s in batch.samples} == {first, origin}
    assert batch.skipped[0].origin_date == last and batch.skipped[0].reason == SkipReason.INCOMPLETE_ACTUALS


@pytest.mark.parametrize("factor", [.5, 2.0])
def test_future_sales_change_error_but_never_the_prediction(hand_case, factor):
    frame, origin, horizon, expected = complete_case(hand_case)
    base = evaluate(frame, origin, horizon).samples[0]
    changed = frame.copy(deep=True)
    changed.loc[changed.stock_date > origin, "outbound_qty"] *= factor
    sample, = evaluate(changed, origin, horizon).samples
    assert sample.forecast_qty == base.forecast_qty
    assert sample.actual_qty == pytest.approx(expected * factor)
    assert sample.actual_to_forecast_ratio == pytest.approx(factor)
    assert (sample.error_qty > 0) == (factor > 1)
    assert sample.absolute_error == pytest.approx(abs(expected * (factor - 1)))


def test_data_after_target_end_cannot_change_even_eligibility(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    end = origin + timedelta(days=horizon)
    options = dict(period=Period(start=origin, end=end + timedelta(days=5)),
                   observed_through=end + timedelta(days=5))
    baseline = evaluate(frame, origin, horizon, **options)
    poison = pd.DataFrame([{"product_id": 101, "stock_date": end + timedelta(days=1),
                            "outbound_qty": float("nan"), "is_valid_observation": "잘못된 미래 표식"}])
    assert evaluate(pd.concat([frame, poison], ignore_index=True), origin, horizon, **options) == baseline


def test_observed_through_prevents_using_present_but_not_yet_known_future(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    batch = evaluate(frame, origin, horizon, observed_through=origin + timedelta(days=horizon - 1))
    assert not batch.samples
    skipped, = batch.skipped
    assert skipped.reason == SkipReason.NOT_OBSERVED_YET and skipped.forecast_qty is None


@pytest.mark.parametrize("origins_offset", [-1, 1])
def test_period_edges_never_truncate_a_target_window(hand_case, origins_offset):
    frame, origin, horizon, _ = complete_case(hand_case)
    batch = evaluate(frame, origin, horizon, origins=[origin + timedelta(days=origins_offset)])
    assert not batch.samples and batch.skipped[0].reason == SkipReason.OUTSIDE_PERIOD


@pytest.mark.parametrize("phase", [SamplePeriod.TUNE, SamplePeriod.TEST])
def test_period_identity_and_exact_end_are_preserved(hand_case, phase):
    frame, origin, horizon, _ = complete_case(hand_case)
    batch = evaluate(frame, origin, horizon, period_name=phase)
    assert batch.period == phase and all(s.period == phase for s in batch.samples)
    assert batch.samples[0].target_end == batch.period_end


def test_missing_actual_day_is_not_zero_filled(hand_case):
    frame, origin, horizon, expected = complete_case(hand_case)
    frame = frame[frame.stock_date != origin + timedelta(days=1)]
    batch = evaluate(frame, origin, horizon)
    assert not batch.samples
    assert batch.skipped[0].reason == SkipReason.INCOMPLETE_ACTUALS
    assert batch.skipped[0].forecast_qty == pytest.approx(expected)


def test_unobserved_target_is_not_treated_as_zero_demand(hand_case):
    frame, origin, horizon, expected = complete_case(hand_case)
    frame.loc[frame.stock_date == origin + timedelta(days=1), "is_valid_observation"] = False
    frame.loc[frame.stock_date > origin, "outbound_qty"] = 0
    batch = evaluate(frame, origin, horizon)
    assert not batch.samples and batch.skipped[0].reason == SkipReason.UNOBSERVED_ACTUALS
    assert batch.skipped[0].forecast_qty == pytest.approx(expected)


@pytest.mark.parametrize("target", [False, True])
def test_duplicate_product_date_is_not_summed_or_selected(hand_case, target):
    frame, origin, horizon, _ = complete_case(hand_case)
    duplicate = frame.loc[frame.stock_date > origin if target else frame.stock_date <= origin].iloc[[0]]
    batch = evaluate(pd.concat([frame, duplicate], ignore_index=True), origin, horizon)
    assert not batch.samples and batch.skipped[0].reason == SkipReason.DUPLICATE_RECORDS


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_invalid_actual_quantity_has_a_reason_and_no_sample(hand_case, value):
    frame, origin, horizon, _ = complete_case(hand_case)
    frame["outbound_qty"] = frame.outbound_qty.astype(object)
    frame.loc[frame.stock_date == origin + timedelta(days=1), "outbound_qty"] = value
    batch = evaluate(frame, origin, horizon)
    assert not batch.samples and batch.skipped[0].reason == SkipReason.INVALID_ACTUALS


@pytest.mark.parametrize("target", [False, True])
def test_non_boolean_observation_flag_is_not_coerced(hand_case, target):
    frame, origin, horizon, _ = complete_case(hand_case)
    frame["is_valid_observation"] = frame.is_valid_observation.astype(object)
    day = origin + timedelta(days=1) if target else origin
    frame.loc[frame.stock_date == day, "is_valid_observation"] = "false"
    batch = evaluate(frame, origin, horizon)
    assert not batch.samples
    assert batch.skipped[0].reason == (SkipReason.INVALID_ACTUALS if target else SkipReason.INVALID_HISTORY)


def test_excluded_training_quantity_does_not_become_an_observation(hand_case):
    frame, origin, horizon, expected = complete_case(hand_case)
    excluded = (frame.stock_date <= origin) & ~frame.is_valid_observation
    assert excluded.any()
    frame.loc[excluded, "outbound_qty"] = float("nan")
    sample, = evaluate(frame, origin, horizon).samples
    assert sample.forecast_qty == pytest.approx(expected)


def test_invalid_observed_training_quantity_is_not_forecast(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    frame.loc[frame.stock_date == origin, "outbound_qty"] = float("nan")
    batch = evaluate(frame, origin, horizon)
    assert not batch.samples and batch.skipped[0].reason == SkipReason.INVALID_HISTORY


def test_unavailable_a_is_not_relabelled_as_b(hand_case):
    frame, origin, horizon, expected = complete_case(hand_case, barcode="TEST-0004")
    batch = evaluate(frame, origin, horizon, methods=[Method.A, Method.B])
    sample, = batch.samples
    skipped, = batch.skipped
    assert sample.method == Method.B and sample.forecast_qty == pytest.approx(expected)
    assert skipped.method == Method.A and skipped.reason == SkipReason.FORECAST_UNAVAILABLE and skipped.detail


@pytest.mark.parametrize("actual_zero", [True, False])
def test_zero_forecast_keeps_additive_error_without_a_ratio(hand_case, actual_zero):
    frame, origin, horizon, expected = complete_case(hand_case)
    frame.loc[frame.stock_date <= origin, "outbound_qty"] = 0
    if actual_zero:
        frame.loc[frame.stock_date > origin, "outbound_qty"] = 0
    sample, = evaluate(frame, origin, horizon).samples
    assert sample.forecast_qty == 0 and sample.actual_to_forecast_ratio is None
    assert sample.ratio_unavailable_reason == "ZERO_FORECAST"
    assert sample.actual_qty == pytest.approx(0 if actual_zero else expected)
    assert sample.error_qty == sample.actual_qty


def test_zero_actual_is_a_valid_ratio_and_negative_error(hand_case):
    frame, origin, horizon, expected = complete_case(hand_case)
    frame.loc[frame.stock_date > origin, "outbound_qty"] = 0
    sample, = evaluate(frame, origin, horizon).samples
    assert sample.actual_qty == 0 and sample.actual_to_forecast_ratio == 0
    assert sample.error_qty == pytest.approx(-expected) and sample.ratio_unavailable_reason is None


def test_nonfinite_ratio_does_not_enter_risk_samples_as_infinity(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    frame.loc[frame.stock_date <= origin, "outbound_qty"] = 1e-310
    sample, = evaluate(frame, origin, horizon).samples
    assert sample.actual_to_forecast_ratio is None and sample.ratio_unavailable_reason == "NONFINITE_RATIO"


def test_actual_sum_overflow_has_no_sample(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    frame.loc[frame.stock_date > origin, "outbound_qty"] = 1e308
    batch = evaluate(frame, origin, horizon)
    assert not batch.samples and batch.skipped[0].reason == SkipReason.INVALID_ACTUALS


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_forecast_does_not_become_an_error_sample(hand_case, monkeypatch, value):
    frame, origin, horizon, _ = complete_case(hand_case)
    monkeypatch.setattr(backtest, "forecast_b", lambda *args: ForecastB(value))
    batch = evaluate(frame, origin, horizon)
    assert not batch.samples and batch.skipped[0].reason == SkipReason.INVALID_FORECAST


def test_products_and_order_are_isolated_and_deterministic(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    other = frame.assign(product_id=202)
    combined = pd.concat([frame, other], ignore_index=True)
    args = dict(product_ids=[101, 202], methods=[Method.A, Method.B])
    baseline = evaluate(combined, origin, horizon, **args)
    assert {s.product_id for s in baseline.samples} == {101, 202}
    assert evaluate(combined.iloc[::-1], origin, horizon, product_ids=[202, 101],
                    methods=[Method.B, Method.A]) == baseline
    poison = other.copy()
    poison["stock_date"] = "다른 상품의 무효 날짜"
    poison["outbound_qty"] = float("nan")
    assert evaluate(pd.concat([frame, poison], ignore_index=True), origin, horizon) == evaluate(frame, origin, horizon)


def test_missing_requested_product_has_unavailable_forecasts(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    batch = evaluate(frame, origin, horizon, product_ids=[999])
    assert not batch.samples
    assert batch.skipped[0].reason == SkipReason.FORECAST_UNAVAILABLE and batch.skipped[0].product_id == 999


def test_future_only_products_do_not_enter_the_automatic_product_universe(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    future = pd.DataFrame([{"product_id": 202, "stock_date": origin + timedelta(days=horizon + 1),
                            "outbound_qty": 0, "is_valid_observation": True}])
    baseline = evaluate(frame, origin, horizon, product_ids=None)
    assert evaluate(pd.concat([frame, future], ignore_index=True), origin, horizon, product_ids=None) == baseline


@pytest.mark.parametrize("override", [
    {"horizon_days": 0}, {"horizon_days": True}, {"horizon_days": 1.5},
    {"period": Period(start=date(2026, 7, 1), end=date(2026, 6, 1))},
    {"period_name": "MIXED"}, {"methods": []}, {"methods": [Method.B, Method.B]}, {"methods": ["C"]},
    {"origins": [date(2026, 6, 15), date(2026, 6, 15)]}, {"origins": [datetime(2026, 6, 15)]},
    {"observed_through": datetime(2026, 6, 25)}, {"product_ids": [101, 101]},
    {"product_ids": [True]}, {"product_ids": [101.0]},
])
def test_invalid_sampling_configuration_is_rejected(hand_case, override):
    frame, origin, horizon, _ = complete_case(hand_case)
    with pytest.raises(BacktestInputError):
        evaluate(frame, origin, horizon, **override)


def test_missing_columns_or_ambiguous_column_names_are_rejected(hand_case):
    frame, origin, horizon, _ = complete_case(hand_case)
    for invalid in [frame.drop(columns="outbound_qty"), pd.concat([frame, frame[["outbound_qty"]]], axis=1)]:
        with pytest.raises(BacktestInputError):
            evaluate(invalid, origin, horizon)
