"""DA-13 관측일·커버 실적에 근거한 등급과 경험 출고 범위."""
import math
from numbers import Integral

from schemas.base import AccuracyGrade


def grade(valid_observation_days: int, cover_rate: float | None) -> AccuracyGrade | None:
    """7일 미만 None, 7~13 LOW, ≥28일이고 커버≥.9 HIGH, 나머지 MEDIUM(일, 비율)."""
    if (not isinstance(valid_observation_days, Integral) or isinstance(valid_observation_days, bool)
            or valid_observation_days < 0 or cover_rate is not None
            and (not math.isfinite(cover_rate) or not 0 <= cover_rate <= 1)):
        raise ValueError("관측일·커버 비율을 확인해 주세요")
    if valid_observation_days < 7:
        return None
    if valid_observation_days < 14:
        return AccuracyGrade.LOW
    if valid_observation_days >= 28 and cover_rate is not None and cover_rate >= .9:
        return AccuracyGrade.HIGH
    return AccuracyGrade.MEDIUM


def empirical_range(forecast_qty: float, ratios: tuple[float, ...]) -> tuple[float, float]:
    """예측(개)×동일 산식/기간 비율의 선형 보간 P10/P90. 중앙80% 경험 범위(개), 신뢰구간 아님."""
    if not ratios or not math.isfinite(forecast_qty) or forecast_qty < 0 or any(
        not math.isfinite(r) or r < 0 for r in ratios
    ):
        raise ValueError("유한한 예측·비율이 필요해요")
    ordered = sorted(ratios)
    def quantile(p):
        rank = (len(ordered) - 1) * p
        lo = math.floor(rank)
        hi = math.ceil(rank)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)
    return forecast_qty * quantile(.1), forecast_qty * quantile(.9)
