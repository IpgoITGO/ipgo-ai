"""DA-06 시나리오 시뮬레이션 · DA-07 품절 위험 추정."""
from dataclasses import dataclass
from math import isfinite

from engine.replenish import start_stock


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


@dataclass(frozen=True)
class StockoutRisk:
    pre_arrival_risk: float | None
    post_arrival_risk: float | None
    sample_size: int


class RiskInputError(ValueError):
    """표본 부족과 구별해야 하는 잘못된 위험 계산 입력."""


def _quantity(value: float) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0
    except OverflowError:
        return False


def stockout_risk(
    error_ratios: tuple[float, ...], min_samples: int, *, as_of_closing: float,
    daily_forecast: float, gap_days: int, horizon_days: int, order_qty: int, lead_time_days: int,
    gap_receipts: dict[int, float], planned_receipts: dict[int, float],
) -> StockoutRisk:
    """고정한 입고량(개)에 비율 r=실적/예측을 재적용한 도착 전/후 위험(0~1).

    각 r마다 일 출고(개/일)×r로 g일 시작 재고를 다시 구하고 H일 DA06을 실행한다.
    pre = count(s+1..s+L-1 미충족>0)/N, post = count(s+L..s+H 미충족>0)/N.
    입고량·도착일·기준일 재고는 고정한다. 기말0만으로 위험 사건을 세지 않는다.
    표본 선택은 engine.risk 책임이며, 부족하면 두 위험 모두 None이다.
    """
    integers = [(min_samples, 1), (gap_days, 0), (horizon_days, 1), (order_qty, 0), (lead_time_days, 1)]
    if (any(type(v) is not int or v < lower for v, lower in integers)
            or lead_time_days > horizon_days or not _quantity(as_of_closing) or not _quantity(daily_forecast)
            or not _quantity(order_qty)):
        raise RiskInputError("표본 기준·기간·재고·예측·입고량을 확인해 주세요")
    for receipts, length in [(gap_receipts, gap_days), (planned_receipts, horizon_days)]:
        if (not isinstance(receipts, dict) or any(
            type(k) is not int or not 1 <= k <= length or not _quantity(q) for k, q in receipts.items()
        )):
            raise RiskInputError("기존 발주는 해당 기간 안의 도착일과 유한한 비음수 수량이 필요해요")
    if any(not _quantity(r) for r in error_ratios):
        raise RiskInputError("오차 비율은 유한한 비음수여야 해요")
    count = len(error_ratios)
    if count < min_samples:
        return StockoutRisk(None, None, count)
    # 0출고는 모든 비율에서 같은 재생이다. 위의 전체 입력 검사는 유지하고 overflow도 실제 재생으로 검사한다.
    replay_ratios = error_ratios[:1] if daily_forecast == 0 else error_ratios
    pre, post = 0, 0
    for ratio in replay_ratios:
        demand = daily_forecast * ratio
        if not isfinite(demand):
            raise RiskInputError("비율을 적용한 예측이 유한한 범위를 넘었어요")
        stock = start_stock(as_of_closing, demand, gap_days, gap_receipts)
        replay = simulate(stock, [demand] * horizon_days, order_qty, lead_time_days, planned_receipts)
        if (not isfinite(stock) or not all(isfinite(v) for v in replay.daily_closing + replay.daily_unmet)
                or not isfinite(replay.unmet_demand)):
            raise RiskInputError("재적용한 재고·미충족 수요가 유한한 범위를 넘었어요")
        pre += replay.pre_arrival_unmet > 0
        post += replay.post_arrival_unmet > 0
    return StockoutRisk(pre / len(replay_ratios), post / len(replay_ratios), count)
