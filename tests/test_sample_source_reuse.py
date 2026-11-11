"""계획별 검증을 유지한 source 준비 재사용을 독립 생성과 비교한다."""
from datetime import date, timedelta

import pandas as pd
import pytest

from engine.backtest import BacktestInputError, generate_error_sample_batches, generate_error_samples
from schemas.engine import Period


def daily_source():
    start = date(2025, 1, 1)
    return pd.DataFrame([{
        "product_id": product, "stock_date": start + timedelta(days=day),
        "outbound_qty": (day % 7) / 10, "is_valid_observation": True,
    } for product in (1, 2) for day in range(577)]).iloc[::-1]


def plan(horizon=10, cutoff=date(2026, 7, 31), **overrides):
    values = dict(origins=[date(2026, 5, 1), date(2026, 5, 8)], horizon_days=horizon,
                  period=Period(start=date(2026, 5, 1), end=date(2026, 7, 31)), period_name="TUNE",
                  observed_through=cutoff, product_ids=[2, 1])
    values.update(overrides)
    return values


@pytest.mark.parametrize("cutoffs,sorts", [
    ([date(2026, 7, 31)] * 4, 2),
    ([date(2026, 5, 11), date(2026, 5, 20), date(2026, 6, 1), date(2026, 6, 2),
      date(2026, 7, 31), date(2026, 5, 11)], 12),
])
def test_sources_are_prepared_once_per_product_cutoff_and_evicted_safely(monkeypatch, cutoffs, sorts):
    daily = daily_source()
    before = daily.copy(deep=True)
    plans = [plan((10, 19, 20, 10)[index % 4], cutoff) for index, cutoff in enumerate(cutoffs)]
    expected = tuple(generate_error_samples(daily, **p) for p in plans)
    original = pd.DataFrame.sort_values
    calls = []

    def record(frame, by, *args, **kwargs):
        if not frame.empty and by == ["product_id", "stock_date"]:
            calls.append(1)
        return original(frame, by, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "sort_values", record)
    assert generate_error_sample_batches(daily, plans) == expected
    assert len(calls) == sorts
    pd.testing.assert_frame_equal(daily, before)


@pytest.mark.parametrize("invalid", [
    {"horizon_days": 0}, {"methods": ["B", "B"]}, {"product_ids": [True]},
    {"observed_through": "2026-07-31"}, {"origins": [date(2026, 5, 1)] * 2},
    {"period_name": "UNKNOWN"},
])
def test_repeated_source_does_not_bypass_plan_validation(invalid):
    daily = daily_source()
    bad = plan(**invalid)
    with pytest.raises(BacktestInputError) as expected:
        generate_error_samples(daily, **bad)
    with pytest.raises(BacktestInputError) as actual:
        generate_error_sample_batches(daily, [plan(), bad])
    assert str(actual.value) == str(expected.value)


def test_unselected_bad_dates_stay_isolated_but_selected_future_bad_dates_fail():
    daily = daily_source()
    daily["stock_date"] = daily.stock_date.astype(object)
    row = daily.index[daily.product_id == 2][0]
    daily.loc[row, "stock_date"] = "잘못된 날짜"
    plans = [plan(10, product_ids=[1]), plan(19, product_ids=[1])]
    assert generate_error_sample_batches(daily, plans) == tuple(
        generate_error_samples(daily, **p) for p in plans
    )
    with pytest.raises(BacktestInputError, match="날짜"):
        generate_error_sample_batches(daily, [*plans, plan(10, product_ids=[2])])


def test_later_cutoff_rechecks_product_types_without_contaminating_earlier_source():
    daily = daily_source()
    daily["product_id"] = daily.product_id.astype(object)
    daily.loc[(daily.product_id == 1) & (daily.stock_date == date(2026, 6, 1)), "product_id"] = True
    earlier = plan(10, date(2026, 5, 20), product_ids=[1])
    later = plan(10, product_ids=[1])
    generate_error_samples(daily, **earlier)
    assert generate_error_sample_batches(daily, [earlier, earlier]) == tuple(
        generate_error_samples(daily, **earlier) for _ in range(2)
    )
    with pytest.raises(BacktestInputError, match="정수"):
        generate_error_sample_batches(daily, [earlier, later])
