"""DA-06 시나리오 시뮬레이션 · DA-07 품절 위험 추정."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Simulation:
    daily_closing: list[float]          # s+1 ~ s+H 기말 재고 (개)
    daily_unmet: list[float]            # s+1 ~ s+H 미충족 수요 (개)
    depletion_day: int | None           # 기말이 처음 0이 되는 날 k (s+k). 커버 기간 안에 없으면 None
    ending_stock: float                 # s+H 기말 재고 (개)
    pre_arrival_unmet: float            # s+1 ~ s+L−1 미충족 합계 (개)
    post_arrival_unmet: float           # s+L ~ s+H 미충족 합계 (개)

    @property
    def unmet_demand(self) -> float:
        return self.pre_arrival_unmet + self.post_arrival_unmet


def simulate(
    start_stock: float,
    daily_forecast: list[float],
    order_qty: int,
    lead_time_days: int,
    planned_receipts: dict[int, float],
) -> Simulation:
    """s+1일 ~ s+H일 날짜별 재고를 계산한다. H = len(daily_forecast), 수량 단위는 개(소수).

    - 신규 입고는 s+리드타임, 기존 발주는 도착일에 더한다 (입고 후 출고 순)
      planned_receipts: {k: 수량} — s+k일 도착. 1 ~ H 밖의 날은 무시 (s까지 도착분은 시작 재고에 포함)
    - 출고 = min(그날 재고, 예상 출고), 미충족 = 예상 출고 − 출고. 부족분은 다음 날로 넘기지 않는다
    """
    stock = start_stock
    closing, unmet = [], []
    for k, demand in enumerate(daily_forecast, 1):
        stock += planned_receipts.get(k, 0) + (order_qty if k == lead_time_days else 0)
        shipped = min(stock, demand)
        stock -= shipped
        closing.append(stock)
        unmet.append(demand - shipped)
    depletion = next((k for k, c in enumerate(closing, 1) if c <= 0), None)
    before = max(lead_time_days - 1, 0)
    return Simulation(
        daily_closing=closing,
        daily_unmet=unmet,
        depletion_day=depletion,
        ending_stock=closing[-1] if closing else start_stock,
        pre_arrival_unmet=sum(unmet[:before]),
        post_arrival_unmet=sum(unmet[before:]),
    )


def stockout_risk(error_ratios: list[float], min_samples: int) -> float | None:
    """5~7월 예측 오차 비율을 시나리오에 적용해 재고 부족 비율을 낸다. 표본 부족이면 None."""
    raise NotImplementedError("DA-07 stockout_risk")
