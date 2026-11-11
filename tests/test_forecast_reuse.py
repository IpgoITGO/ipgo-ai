"""기간 집계 재사용과 독립 생성의 결과·검증 경계가 같은지 확인한다."""
from datetime import date, timedelta

import pandas as pd
import pytest

from engine.backtest import generate_error_sample_batches, generate_error_samples
from engine.forecast import _PreparedForecast, forecast_a, forecast_b
from schemas.engine import Period


@pytest.mark.parametrize("origin", [date(2024, 2, 29), date(2025, 3, 1)])
@pytest.mark.parametrize("horizon", [1, 19, 366, 800])
@pytest.mark.parametrize("bounds", [None, (0.5, 2.0)])
def test_reused_windows_preserve_fractional_sums_and_date_cutoff(origin, horizon, bounds):
    start = date(2023, 1, 1)
    quantities = [1e16, 0.1, 1e-12, 3.75, 0.0]
    daily = pd.DataFrame([{
        "stock_date": start + timedelta(days=n),
        "outbound_qty": quantities[n % len(quantities)],
        "is_valid_observation": n % 11 != 0,
    } for n in range((date(2026, 1, 1) - start).days)])
    prepared = _PreparedForecast(daily)
    # 먼저 늦은 기준일을 평가해도 이후 이른 기준일에 미래 정보를 섞지 않는다.
    for cutoff in (origin, origin - timedelta(days=7), origin):
        assert prepared.forecast_a(cutoff, horizon, bounds) == forecast_a(daily, cutoff, horizon, bounds)
        assert prepared.forecast_b(cutoff, horizon) == forecast_b(daily, cutoff, horizon)


@pytest.mark.parametrize("poison", ["negative", "nan", "flag", "duplicate", "unobserved"])
def test_batched_reuse_keeps_independent_validation_and_sample_order(poison):
    start = date(2025, 1, 1)
    daily = pd.DataFrame([{
        "product_id": product, "stock_date": start + timedelta(days=n),
        "outbound_qty": (n % 9) / 10, "is_valid_observation": True,
    } for product in (1, 2) for n in range(570)])
    at = (daily.product_id == 1) & (daily.stock_date == date(2026, 5, 5))
    if poison == "duplicate":
        daily = pd.concat([daily, daily[at]], ignore_index=True)
    elif poison == "flag":
        daily["is_valid_observation"] = daily.is_valid_observation.astype(object)
        daily.loc[at, "is_valid_observation"] = "true"
    elif poison == "unobserved":
        daily.loc[at, "is_valid_observation"] = False
        daily.loc[at, "outbound_qty"] = float("nan")
    else:
        daily.loc[at, "outbound_qty"] = -1 if poison == "negative" else float("nan")
    plans = [{
        "origins": [date(2026, 5, 1), date(2026, 5, 8)], "horizon_days": horizon,
        "period": Period(start=date(2026, 5, 1), end=date(2026, 7, 31)),
        "period_name": "TUNE", "observed_through": date(2026, 7, 31), "product_ids": [2, 1],
    } for horizon in (10, 19)]
    expected = tuple(generate_error_samples(daily, **plan) for plan in plans)
    assert generate_error_sample_batches(daily.iloc[::-1], plans) == expected
    # 새 요청의 관측값을 이전 요청의 집계로 대체하지 않는다.
    changed = daily.copy()
    changed["outbound_qty"] *= 2
    assert generate_error_sample_batches(changed, plans) == tuple(
        generate_error_samples(changed, **plan) for plan in plans
    )
