"""위험 후보·0출고 재생 재사용의 결과와 오류 경계를 독립 경로와 비교한다."""
from dataclasses import replace
from datetime import timedelta

import pytest

from engine import scenario
from engine.risk import _RiskSelector, select_risk_samples
from engine.scenario import RiskInputError, stockout_risk
from schemas.base import Method
from tests.test_risk import END, START, batch, context, sample


@pytest.mark.parametrize("minimum", [1, 2, 20])
def test_cached_selection_matches_independent_selection_for_all_conditions(minimum):
    source = context(
        sample(0), sample(1, origin=START + timedelta(days=1)), sample(2, product_id=202),
        sample(1, method=Method.A), sample(1, horizon=19), minimum=minimum,
    )
    selector = _RiskSelector(source)
    for product in (101, 202, 999, 101):
        for method in (Method.A, Method.B):
            for horizon in (10, 19):
                for cutoff in (START + timedelta(days=10), END):
                    args = dict(product_id=product, method=method, horizon_days=horizon, observed_through=cutoff)
                    assert selector.select(**args) == select_risk_samples(source, **args)
                    assert len(selector._candidates) <= 4
    other = context(sample(10), minimum=minimum)
    args = dict(product_id=101, method=Method.B, horizon_days=10, observed_through=END)
    assert _RiskSelector(other).select(**args) == select_risk_samples(other, **args)


@pytest.mark.parametrize("problem", ["ratio", "date", "duplicate"])
def test_cache_preserves_lazy_errors_in_other_conditions(problem):
    bad = sample(method=Method.A, horizon=19)
    if problem == "ratio":
        bad = replace(bad, actual_to_forecast_ratio=float("nan"))
    elif problem == "date":
        bad = replace(bad, target_end=END)
    batches = (batch(sample(), bad),) if problem != "duplicate" else (batch(sample(), bad), batch(bad))
    source = context(batches=batches)
    selector = _RiskSelector(source)
    args = dict(product_id=101, method=Method.B, horizon_days=10, observed_through=END)
    assert selector.select(**args) == select_risk_samples(source, **args)
    invalid = {**args, "method": Method.A, "horizon_days": 19}
    for select in (selector.select, lambda **kwargs: select_risk_samples(source, **kwargs)):
        with pytest.raises(RiskInputError):
            select(**invalid)
    assert selector.select(**args) == select_risk_samples(source, **args)


@pytest.mark.parametrize("override", [
    {"product_id": True}, {"method": "B"}, {"horizon_days": True}, {"observed_through": "2026-07-31"},
])
def test_cache_hit_does_not_bypass_request_validation(override):
    selector = _RiskSelector(context(sample()))
    args = dict(product_id=101, method=Method.B, horizon_days=10, observed_through=END)
    selector.select(**args)
    with pytest.raises(RiskInputError):
        selector.select(**{**args, **override})


def zero_args():
    return dict(as_of_closing=12, daily_forecast=0.0, gap_days=2, horizon_days=3,
                order_qty=4, lead_time_days=2, gap_receipts={1: 2}, planned_receipts={3: 3})


@pytest.mark.parametrize("forecast", [0.0, -0.0])
@pytest.mark.parametrize("lead", [1, 3])
def test_zero_forecast_replays_once_and_preserves_original_sample_size(monkeypatch, forecast, lead):
    calls = []
    original = scenario.simulate

    def track(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(scenario, "simulate", track)
    args = zero_args()
    args.update(daily_forecast=forecast, lead_time_days=lead)
    result = stockout_risk((0.0, 1.0, 1e300), 2, **args)
    assert result.pre_arrival_risk == result.post_arrival_risk == 0.0
    assert result.sample_size == 3
    assert len(calls) == 1


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True, None])
def test_zero_forecast_checks_every_ratio_even_below_minimum(invalid):
    with pytest.raises(RiskInputError):
        stockout_risk((0.0, invalid), 3, **zero_args())


@pytest.mark.parametrize("in_gap", [False, True])
def test_zero_forecast_still_rejects_overflow_in_replayed_stock(in_gap):
    args = zero_args()
    args.update(as_of_closing=1e308)
    args["gap_receipts" if in_gap else "planned_receipts"] = {1: 1e308}
    with pytest.raises(RiskInputError, match="유한"):
        stockout_risk((0.0, 1.0), 1, **args)


@pytest.mark.parametrize("ratios", [(), (0.0,)])
def test_zero_forecast_insufficient_samples_are_not_zero_risk(monkeypatch, ratios):
    monkeypatch.setattr(scenario, "simulate", lambda *_args, **_kwargs: pytest.fail("표본 부족인데 재생"))
    result = stockout_risk(ratios, 2, **zero_args())
    assert result.pre_arrival_risk is result.post_arrival_risk is None
    assert result.sample_size == len(ratios)
