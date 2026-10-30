"""DA-03 커버 기간 출고량 예측. 결과는 소수(float)."""
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

MIN_VALID_DAYS = 7      # 기준 B: 이보다 적으면 사용 불가
B_WINDOW_MAX = 28       # 기준 B 최대 구간 (기준일 포함)
A_WINDOW_DAYS = 90      # [가정] 계산식 A 직전 구간 길이 (기획서 [가정: 90일])
A_MIN_VALID_DAYS = 7    # [가정] 계산식 A 세 구간 각각의 최소 유효일 (5~7월 조정 데이터로 결정)
TREND_BOUNDS: tuple[float, float] | None = None  # [가정] 추세 비율 상 · 하한 (5~7월 조정 데이터로 결정)


@dataclass(frozen=True)
class ForecastB:
    qty: float | None                   # D일 예상 출고 합계 (개). 사용 불가면 None
    reason: str | None = None           # 사용 불가 사유
    valid_days: int = 0
    valid_outbound: float = 0.0
    daily_mean: float | None = None


@dataclass(frozen=True)
class ForecastA:
    qty: float | None                   # D일 예상 출고 합계 (개). 사용 불가면 None
    reason: str | None = None           # 사용 불가 사유
    p_h: float | None = None            # 작년 예측 구간 유효일 평균 (개/일)
    c_90: float | None = None           # 올해 직전 90일 유효일 평균 (개/일)
    p_90: float | None = None           # 작년 같은 90일 유효일 평균 (개/일)
    trend: float | None = None          # C_90 ÷ P_90 (상 · 하한 적용 후)


def forecast_a(
    product_daily: pd.DataFrame,
    as_of: date,
    forecast_days: int,
    trend_bounds: tuple[float, float] | None = TREND_BOUNDS,
) -> ForecastA:
    """계산식 A = P_H × D × (C_90 ÷ P_90). 평균은 모두 유효 관측일만으로 낸다 (개/일).

    P_H: 작년 t+1 ~ t+D, C_90: 올해 t−89 ~ t, P_90: 작년 t−89 ~ t (작년 = 같은 월 · 일).
    세 구간 중 하나라도 유효일이 A_MIN_VALID_DAYS 미만이거나 P_90 = 0이면 사용 불가.
    product_daily: 한 상품의 stock_date · outbound_qty · is_valid_observation (DA-02). t 이후 기록은 쓰지 않는다.
    """
    daily = _until(product_daily, as_of)
    last_year = _last_year(as_of)
    windows = {
        "P_H": (last_year + timedelta(days=1), last_year + timedelta(days=forecast_days)),
        "C_90": (as_of - timedelta(days=A_WINDOW_DAYS - 1), as_of),
        "P_90": (last_year - timedelta(days=A_WINDOW_DAYS - 1), last_year),
    }
    means = {}
    for name, (start, end) in windows.items():
        days, total = _valid_sum(daily, start, end)
        if days < A_MIN_VALID_DAYS:
            reason = f"{name} 구간({start} ~ {end}) 판매한 날이 {days}일로 {A_MIN_VALID_DAYS}일보다 적어요"
            return ForecastA(None, reason)
        means[name] = total / days
    p_h, c_90, p_90 = means["P_H"], means["C_90"], means["P_90"]
    if p_90 == 0:
        return ForecastA(None, "작년 같은 90일 평균 출고가 0이라 추세를 구할 수 없어요", p_h, c_90, p_90)
    trend = c_90 / p_90
    if trend_bounds is not None:
        trend = min(max(trend, trend_bounds[0]), trend_bounds[1])
    return ForecastA(p_h * forecast_days * trend, None, p_h, c_90, p_90, trend)


def forecast_b(product_daily: pd.DataFrame, as_of: date, forecast_days: int) -> ForecastB:
    """기준 B = 최근 28일(기준일 포함) 유효일 출고 합계 ÷ 유효일 수 × D (개).

    기록이 28일보다 짧으면 있는 만큼으로 계산. 유효일이 7일 미만이면 사용 불가. t 이후 기록은 쓰지 않는다.
    """
    daily = _until(product_daily, as_of)
    days, total = _valid_sum(daily, as_of - timedelta(days=B_WINDOW_MAX - 1), as_of)
    if days < MIN_VALID_DAYS:
        reason = f"최근 {B_WINDOW_MAX}일 중 판매한 날(유효 관측일)이 {days}일로 {MIN_VALID_DAYS}일보다 적어요"
        return ForecastB(None, reason, days, total)
    mean = total / days
    return ForecastB(mean * forecast_days, None, days, total, mean)


def _until(product_daily: pd.DataFrame, as_of: date) -> pd.DataFrame:
    return product_daily[product_daily["stock_date"] <= as_of]


def _valid_sum(daily: pd.DataFrame, start: date, end: date) -> tuple[int, float]:
    """start ~ end(포함) 유효일 수와 그날들의 출고 합계."""
    rows = daily[(daily["stock_date"] >= start) & (daily["stock_date"] <= end) & daily["is_valid_observation"]]
    return len(rows), float(rows["outbound_qty"].sum())


def _last_year(day: date) -> date:
    """작년 같은 월 · 일. 2/29는 2/28."""
    if day.month == 2 and day.day == 29:
        return day.replace(year=day.year - 1, day=28)
    return day.replace(year=day.year - 1)
