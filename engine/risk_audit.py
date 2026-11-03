"""DA07 최소 표본 후보의 가용성을 비교한다. 후보를 자동 채택하거나 신뢰도를 꾸미지 않는다."""
from collections import Counter
from datetime import date
from statistics import median

import pandas as pd

from engine.backtest import SamplePeriod, generate_error_samples
from engine.risk import RiskContext
from schemas.base import Method
from schemas.engine import Period


def _non_overlapping(samples) -> int:
    """(기준일, 종료일]이 겹치지 않는 최대 창 개수. 독립성/유효 표본 크기를 뜻하지 않는다."""
    end, count = None, 0
    for sample in sorted(samples, key=lambda s: (s.target_end, s.origin_date)):
        if end is None or sample.origin_date >= end:
            end = sample.target_end
            count += 1
    return count


def audit_risk_samples(
    product_daily: pd.DataFrame, *, period: Period, observed_through: date,
    origins: tuple[date, ...], horizons: tuple[int, ...], candidates: tuple[int, ...],
) -> dict:
    """같은5~7월의 A/B·D별 후보를 집계한다. 수량은 DA08 관측 출고, 비율은 실적/예측이다.

    후보N의 경험 비율 한 칸은100/N %p다. 독립 표본·오차 허용폭·보정된 품절 확률을 보장하지 않는다.
    상품 식별자·원본 행·타깃 TEST 결과를 보고서에 넣지 않는다.
    """
    if not candidates or len(set(candidates)) != len(candidates):
        raise ValueError("최소 표본 후보는 중복 없이 하나 이상 필요해요")
    for minimum in candidates:
        RiskContext((), period.start, period.end, minimum)
    if (not horizons or any(type(d) is not int or d < 1 for d in horizons)
            or len(set(horizons)) != len(horizons)):
        raise ValueError("예측 길이는 중복 없는 양의 정수여야 해요")
    # 작년 비교에만 등장한 상품을 현재 조정 구간 상품 수/coverage 분모에 넣지 않는다.
    current = product_daily[
        (product_daily["stock_date"] >= period.start)
        & (product_daily["stock_date"] <= min(period.end, observed_through))
    ]
    current_ids = list(current["product_id"].unique())
    profiles = []
    for days in sorted(horizons):
        batch = generate_error_samples(
            product_daily, origins=origins, horizon_days=days, period=period,
            period_name=SamplePeriod.TUNE, observed_through=observed_through,
            product_ids=current_ids,
        )
        ids = {s.product_id for s in batch.samples} | {s.product_id for s in batch.skipped}
        for method in (Method.A, Method.B):
            samples = [s for s in batch.samples if s.method == method]
            usable = [s for s in samples if s.actual_to_forecast_ratio is not None]
            counts = Counter(s.product_id for s in usable)
            reasons = Counter(s.reason.value for s in batch.skipped if s.method == method)
            reasons.update(s.ratio_unavailable_reason for s in samples if s.ratio_unavailable_reason)
            raw_counts = [counts.get(p, 0) for p in ids]
            nonoverlap = [_non_overlapping([s for s in usable if s.product_id == p]) for p in ids]
            choices = []
            for minimum in sorted(candidates):
                own = sum(n >= minimum for n in raw_counts)
                pooled = len(usable) >= minimum
                choices.append({
                    "minimum": minimum, "nominal_step_percent_points": 100 / minimum,
                    "sku_count": own, "pooled_count": len(ids) - own if pooled else 0,
                    "insufficient_count": 0 if pooled else len(ids) - own,
                })
            profiles.append({
                "method": method.value, "forecast_days": days, "products": len(ids),
                "usable_ratio_cases": len(usable), "excluded_reasons": dict(sorted(reasons.items())),
                "sku_cases": {"min": min(raw_counts, default=0), "median": median(raw_counts) if raw_counts else 0,
                              "max": max(raw_counts, default=0)},
                "non_overlapping_windows_sum": sum(nonoverlap),
                "candidates": choices,
            })
    return {
        "period": {"start": str(period.start), "end": str(period.end), "observed_through": str(observed_through)},
        "origins": [str(d) for d in sorted(origins)], "profiles": profiles,
        "selected_min_samples": None,
        "limitations": ["관측 출고 기반, 전체 창 유효 관측만 사용", "겹치지 않는 창도 SKU 간 독립성을 보장하지 않음",
                        "가용성/분해능 비교만으로 품절 위험 신뢰도나 최소 기준을 자동 확정하지 않음"],
    }
