"""DA-13 SKU별 정확도 등급 (기준은 팀 결정 예시)."""
from schemas.base import AccuracyGrade


def grade(valid_observation_days: int, cover_rate: float | None) -> AccuracyGrade | None:
    """7일 미만 None(계산 불가) · 7~13일 LOW · 14~27일 또는 버틴 비율 90% 미만 MEDIUM · 그 외 HIGH."""
    raise NotImplementedError("DA-13 grade")
