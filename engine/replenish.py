"""DA-04 입고 수량 산정 · DA-05 소진 예상일과 입고 필요 판정."""
import math
from datetime import date


def required_qty(
    forecast_qty: float,
    multiplier: float,
    start_stock: float,
    planned_in_horizon: float,
    pre_arrival_unmet: float,
) -> float:
    """올림 전 필요량 = max(0, 예상 출고 × 배수 − 시작 재고 − 기존 발주 − 도착 전 미충족 수요)."""
    raise NotImplementedError("DA-04 required_qty")


def round_to_order_unit(qty: float, order_unit_qty: int) -> int:
    """입고 단위로 올림한다. 예: 66.3개, 단위 12개 → 72개."""
    if qty <= 0:
        return 0
    return math.ceil(qty / order_unit_qty) * order_unit_qty


def expected_shortage_on(as_of: date, today: date, closing_stock: float, daily_forecast: float) -> date | None:
    """기준일 종료 재고에서 예측 출고를 이어 빼 재고가 0이 되는 날. 산출 불가면 None."""
    raise NotImplementedError("DA-05 expected_shortage_on")
