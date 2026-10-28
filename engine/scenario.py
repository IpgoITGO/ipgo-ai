"""DA-06 시나리오 시뮬레이션 · DA-07 품절 위험 추정."""


def simulate(
    start_stock: float,
    daily_forecast: list[float],
    order_qty: int,
    lead_time_days: int,
    planned_receipts: dict[int, float],
) -> dict:
    """s+1일 ~ s+H일 날짜별 재고를 계산한다.

    - 신규 입고는 s+리드타임, 기존 발주는 도착일에 더한다 (입고 후 출고 순)
    - 부족분은 미충족 수요로 기록하고 다음 날로 넘기지 않는다
    반환(제안): daily_closing, depletion_day, ending_stock, unmet_demand
    """
    raise NotImplementedError("DA-06 simulate")


def stockout_risk(error_ratios: list[float], min_samples: int) -> float | None:
    """5~7월 예측 오차 비율을 시나리오에 적용해 재고 부족 비율을 낸다. 표본 부족이면 None."""
    raise NotImplementedError("DA-07 stockout_risk")
