"""DA-11 → AI-01: 한 상품의 계산 사실을 이름 목록과 비교 방향으로 바꾼다 (#36).

값 삽입·표시 문구는 Spring 책임이다. 위험/정확도 산출이나 기본 문장 생성은 하지 않는다.
"""
import math
from datetime import date
from typing import Literal

from schemas.base import ResultStatus, RiskStatus, ScenarioType
from schemas.engine import ProductResult, ScenarioResult
from schemas.insight import InsightInput

Direction = Literal["높음", "낮음", "같음"]


def _number(value: float | int | None, *, positive: bool = False) -> bool:
    return (value is not None and not isinstance(value, bool) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def _described(result: ProductResult) -> list[ScenarioResult]:
    """권장량/P80의 존재를 확인하고 중복 표시는 설명 대상에서 뺀다. 같은 type 두 건은 해석하지 않는다."""
    by_type = {s.type: s for s in result.scenarios}
    base = by_type.get(ScenarioType.P100)
    if (len(by_type) != len(result.scenarios) or base is None or base.is_duplicate
            or base.qty != result.recommended_qty or ScenarioType.P80 not in by_type):
        return []
    return [by_type[kind] for kind in ScenarioType if kind in by_type and not by_type[kind].is_duplicate]


def build_catalog(result: ProductResult, *, as_of_date: date | None = None) -> list[str]:
    """값이 있는 키 이름만 만든다 (prompt 문서5/5-1). 필수 사실이 없으면 빈 목록이다.

    † 문구 키(label/action/dir/formula_desc/risk_basis)는 FastAPI가 유도하며 여기에는 넣지 않는다.
    소진일 None은 '기간 안에 소진되지 않음'이라는 계산 사실이므로 depletion 키를 제공한다.
    """
    if (result.status != ResultStatus.OK or result.risk_status is None or result.method is None
            or result.method_fallback is None or not _number(result.recommended_qty, positive=True)):
        return []
    scenarios = _described(result)
    if not scenarios:
        return []
    if result.method_fallback and not (result.method_reason and result.method_reason.strip()):
        return []
    if result.start_stock_estimated and (as_of_date is None or result.start_stock_qty is None):
        return []

    keys = ["method"]
    if result.method_fallback:
        keys.append("method_reason")
    for key, value in (
        ("horizon_days", result.horizon_days), ("start_stock", result.start_stock_qty),
        ("planned_receipts", result.planned_receipts_in_horizon),
        ("forecast_outbound", result.forecast_outbound_qty), ("multiplier", result.multiplier),
    ):
        if value is not None:
            if not _number(value, positive=key in {"horizon_days", "multiplier"}):
                return []
            keys.append(key)
    if result.start_stock_estimated:
        keys.append("as_of_date")
    if result.no_date_receipt_qty > 0:
        keys.append("no_date_receipts")

    risk = result.risk_status == RiskStatus.OK
    if risk:
        if (not _number(result.pre_arrival_risk) or result.risk_basis is None
                or not _number(result.risk_sample_size, positive=True)):
            return []
        keys += ["risk_pre", "risk_sample_size"]
    elif result.risk_status == RiskStatus.LOW_GRADE:
        low, high = result.forecast_range_low, result.forecast_range_high
        if not _number(low) or not _number(high) or low > high:
            return []
        keys += ["forecast_range_low", "forecast_range_high"]

    for scenario in scenarios:
        if (not _number(scenario.qty) or not _number(scenario.expected_ending_stock)
                or not _number(scenario.unmet_demand_qty)):
            return []
        suffix = scenario.type.value.lower()
        keys += [f"{name}_{suffix}" for name in ("qty", "depletion", "ending", "unmet")]
        if scenario.type != ScenarioType.P100:
            keys.append(f"diff_{suffix}")
        if risk:
            if not _number(scenario.post_arrival_risk):
                return []
            keys.append(f"risk_post_{suffix}")
    return keys


def _compare(left: int, right: int) -> Direction:
    return "높음" if left > right else "낮음" if left < right else "같음"


def _percent(value: float) -> int:
    """0~1 비율을 화면의 정수 %로 바꾼다: floor(100 × 비율 + 0.5), prompt 문서6-4."""
    return math.floor(value * 100 + 0.5)


def to_insight_input(
    result: ProductResult, sku_label: str, *, as_of_date: date | None = None,
) -> InsightInput | None:
    """완전한 OK 결과만 변환한다. sku_label은 호출자가 만든 별칭이며 실제 상품명을 주지 않는다.

    startStockEstimated이면 요청의 asOfDate도 필요하다. None 반환은 생성 보류이며 위험 상태를
    표본 부족으로 치환하지 않는다. 원본 result/scenarios에는 쓰지 않고 실제 값·상품 ID도 보내지 않는다.
    """
    if not sku_label.strip():
        return None
    keys = build_catalog(result, as_of_date=as_of_date)
    if not keys:
        return None
    scenarios = _described(result)
    base = next(s for s in scenarios if s.type == ScenarioType.P100)
    directions = {}
    for scenario in scenarios:
        if scenario.type == ScenarioType.P100:
            continue
        suffix = scenario.type.value.lower()
        # 원시 소수는 보존하고 방향만 비교표 표시값(재고 FLOOR, 위험 정수 %)으로 비교한다.
        directions[f"ending_{suffix}_vs_p100"] = _compare(
            math.floor(scenario.expected_ending_stock), math.floor(base.expected_ending_stock),
        )
        if result.risk_status == RiskStatus.OK:
            directions[f"risk_post_{suffix}_vs_p100"] = _compare(
                _percent(scenario.post_arrival_risk), _percent(base.post_arrival_risk),
            )
    return InsightInput(
        sku_label=sku_label, recommended=ScenarioType.P100, risk_status=result.risk_status,
        placeholders=keys, directions=directions, scenarios=[s.type for s in scenarios],
        method_fallback=result.method_fallback, start_stock_estimated=result.start_stock_estimated,
        has_no_date_receipts=result.no_date_receipt_qty > 0,
    )
