"""DA-04 입고 수량 산정 · DA-05 소진 예상일과 입고 필요 판정."""
import math
from datetime import date


def split_forecast(forecast_total: float, gap_days: int, horizon_days: int) -> tuple[float, float]:
    """D일 예상 출고 합계를 (일평균, 커버 기간 예상 출고)로 나눈다.

    D = g + H, 일평균 = 합계 ÷ D (개/일), 커버 기간 예상 출고 = 일평균 × H (개). 앞 g일 몫은 시작 재고 추정에 쓴다.
    """
    daily = forecast_total / (gap_days + horizon_days)
    return daily, daily * horizon_days


def split_planned_receipts(
    receipts: list[tuple[date, float]], as_of: date, today: date, horizon_days: int,
) -> tuple[dict[int, float], dict[int, float]]:
    """기존 발주(도착 예정일, 수량)를 (시작 재고에 더할 것, 커버 기간 안)으로 나눈다.

    t 이하는 이미 수불부에 반영돼 버리고, t+1 ~ s는 {t부터 며칠째: 수량}, s+1 ~ s+H는 {s부터 며칠째: 수량}
    (DA-06 simulate의 planned_receipts), s+H 이후는 버린다. 같은 날 도착분은 더한다.
    """
    before_start: dict[int, float] = {}
    in_horizon: dict[int, float] = {}
    for arrival, qty in receipts:
        if as_of < arrival <= today:
            k = (arrival - as_of).days
            before_start[k] = before_start.get(k, 0) + qty
        elif 0 < (k := (arrival - today).days) <= horizon_days:
            in_horizon[k] = in_horizon.get(k, 0) + qty
    return before_start, in_horizon


def start_stock(as_of_closing: float, daily_forecast: float, gap_days: int, receipts: dict[int, float]) -> float:
    """시작 재고 (개): 기준일 기말에서 t+1 ~ s 하루씩 '그날 도착분 더하기 → 일평균 빼기', 날마다 0 미만이면 0.

    receipts: {t부터 며칠째: 수량} (split_planned_receipts의 첫 번째). gap_days > 0이면 추정 재고다.
    """
    stock = as_of_closing
    for k in range(1, gap_days + 1):
        stock = max(0.0, stock + receipts.get(k, 0) - daily_forecast)
    return stock


def required_qty(
    forecast_qty: float,
    multiplier: float,
    start_stock: float,
    planned_in_horizon: float,
    pre_arrival_unmet: float,
) -> float:
    """올림 전 필요량 (개).

    = max(0, 커버 기간 예상 출고 × 배수 − 시작 재고 − 커버 기간 안 기존 발주 − 도착 전 미충족 수요)
    """
    return max(0.0, forecast_qty * multiplier - start_stock - planned_in_horizon - pre_arrival_unmet)


def round_to_order_unit(qty: float, order_unit_qty: int) -> int:
    """입고 단위로 올림한다. 예: 66.3개, 단위 12개 → 72개."""
    if qty <= 0:
        return 0
    return math.ceil(qty / order_unit_qty) * order_unit_qty


def expected_shortage_on(as_of: date, today: date, closing_stock: float, daily_forecast: float) -> date | None:
    """기준일 종료 재고에서 예측 출고를 이어 빼 재고가 0이 되는 날. 산출 불가면 None."""
    raise NotImplementedError("DA-05 expected_shortage_on")
