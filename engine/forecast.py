"""DA-03 커버 기간 출고량 예측. 결과는 소수(float)."""
from datetime import date

import pandas as pd

MIN_VALID_DAYS = 7      # 이보다 적으면 계산 불가
B_WINDOW_MAX = 28       # 기준 B 최대 구간


def forecast_a(product_daily: pd.DataFrame, as_of: date, horizon_days: int) -> float | None:
    """계산식 A: 작년 동일 커버 기간 출고 × 직전 90일 추세. 작년 자료가 없으면 None."""
    raise NotImplementedError("DA-03 forecast_a")


def forecast_b(product_daily: pd.DataFrame, as_of: date, horizon_days: int) -> float | None:
    """기준 B: 최근 유효 관측일 평균(최소 7일 · 최대 28일) × 커버 기간.

    기준일 as_of 이후 데이터는 쓰지 않는다. 유효 관측일이 7일 미만이면 None.
    """
    raise NotImplementedError("DA-03 forecast_b")
