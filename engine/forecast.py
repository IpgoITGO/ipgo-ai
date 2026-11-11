"""DA-03 커버 기간 출고량 예측. 결과는 소수(float)."""
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

MIN_VALID_DAYS = 7      # 기준 B: 이보다 적으면 사용 불가
B_WINDOW_MAX = 28       # 기준 B 최대 구간 (기준일 포함)
A_WINDOW_DAYS = 90      # [가정] 계산식 A 직전 구간 길이 (기획서 [가정: 90일])
A_MIN_VALID_DAYS = 7    # [가정] 계산식 A 세 구간 각각의 최소 유효일 (5~7월 조정 데이터로 결정)
TREND_BOUNDS: tuple[float, float] | None = None  # [가정] 추세 비율 상 · 하한 (5~7월 조정 데이터로 결정)
# 사용 불가 사유는 화면 · 인사이트 고정 문장에 그대로 보이므로 구간 기호 대신 셀러 말로 쓴다 (#68).
A_WINDOW_LABELS = {"P_H": "작년 같은 기간", "C_90": "최근 90일", "P_90": "작년 같은 날까지 90일"}


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
    return _forecast_a(as_of, forecast_days, trend_bounds, lambda start, end: _valid_sum(daily, start, end))


def _forecast_a(as_of: date, forecast_days: int, trend_bounds: tuple[float, float] | None,
                window_sum: Callable[[date, date], tuple[int, float]]) -> ForecastA:
    """A 출고량(개) = 작년 구간 평균 × 예측 일수 × 올해/작년 90일 평균 비율."""
    last_year = _last_year(as_of)
    windows = {
        "P_H": (last_year + timedelta(days=1), last_year + timedelta(days=forecast_days)),
        "C_90": (as_of - timedelta(days=A_WINDOW_DAYS - 1), as_of),
        "P_90": (last_year - timedelta(days=A_WINDOW_DAYS - 1), last_year),
    }
    means = {}
    for name, (start, end) in windows.items():
        days, total = window_sum(start, end)
        if days < A_MIN_VALID_DAYS:
            reason = f"{A_WINDOW_LABELS[name]}({start} ~ {end}) 판매한 날이 {days}일로 {A_MIN_VALID_DAYS}일보다 적어요"
            return ForecastA(None, reason)
        means[name] = total / days
    p_h, c_90, p_90 = means["P_H"], means["C_90"], means["P_90"]
    if p_90 == 0:
        return ForecastA(None, "작년 같은 날까지 90일 동안 판매 기록이 없어 작년 대비 흐름을 구할 수 없어요",
                         p_h, c_90, p_90)
    trend = c_90 / p_90
    if trend_bounds is not None:
        trend = min(max(trend, trend_bounds[0]), trend_bounds[1])
    return ForecastA(p_h * forecast_days * trend, None, p_h, c_90, p_90, trend)


def forecast_b(product_daily: pd.DataFrame, as_of: date, forecast_days: int) -> ForecastB:
    """기준 B = 최근 28일(기준일 포함) 유효일 출고 합계 ÷ 유효일 수 × D (개).

    기록이 28일보다 짧으면 있는 만큼으로 계산. 유효일이 7일 미만이면 사용 불가. t 이후 기록은 쓰지 않는다.
    """
    daily = _until(product_daily, as_of)
    return _forecast_b(as_of, forecast_days, lambda start, end: _valid_sum(daily, start, end))


def _forecast_b(as_of: date, forecast_days: int,
                window_sum: Callable[[date, date], tuple[int, float]]) -> ForecastB:
    """B 출고량(개) = 최근 28일 유효일 출고 합계 ÷ 유효일 수 × 예측 일수."""
    days, total = window_sum(as_of - timedelta(days=B_WINDOW_MAX - 1), as_of)
    if days < MIN_VALID_DAYS:
        reason = f"최근 {B_WINDOW_MAX}일 중 판매한 날이 {days}일로 {MIN_VALID_DAYS}일보다 적어요"
        return ForecastB(None, reason, days, total)
    mean = total / days
    return ForecastB(mean * forecast_days, None, days, total, mean)


class _PreparedForecast:
    """검증된 한 상품 이력의 기간별 유효일 수·출고 합계(개)를 요청 안에서 재사용한다.

    날짜가 정렬된 숫자 이력을 받으며 호출자는 각 기준일의 수량 검사를 먼저 통과해야 한다.
    조회는 항상 기준일 이하로 잘라 미래 기록을 쓰지 않는다.
    누적합 차감 대신 기존 Series.sum을 같은 순서의 구간에 적용해 소수 합산을 보존한다.
    """

    def __init__(self, history: pd.DataFrame):
        valid = history.loc[history["is_valid_observation"]]
        self._dates = tuple(valid["stock_date"])
        self._outbound = valid["outbound_qty"].reset_index(drop=True)
        self._sums: dict[tuple[date, date], tuple[int, float]] = {}

    def _sum(self, start: date, end: date) -> tuple[int, float]:
        key = (start, end)
        if key not in self._sums:
            left, right = bisect_left(self._dates, start), bisect_right(self._dates, end)
            self._sums[key] = (max(0, right - left), float(self._outbound.iloc[left:right].sum()))
        return self._sums[key]

    def forecast_a(self, as_of: date, forecast_days: int,
                   trend_bounds: tuple[float, float] | None = TREND_BOUNDS) -> ForecastA:
        return _forecast_a(as_of, forecast_days, trend_bounds,
                           lambda start, end: self._sum(start, min(end, as_of)))

    def forecast_b(self, as_of: date, forecast_days: int) -> ForecastB:
        return _forecast_b(as_of, forecast_days, lambda start, end: self._sum(start, min(end, as_of)))


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
