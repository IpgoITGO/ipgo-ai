"""DA07: 명시적으로 고정한 정책으로 요청 source의 DA08 표본을 만든다."""
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from engine.backtest import BacktestInputError, SamplePeriod, generate_error_samples
from engine.calculate import CalculationInputError, _inputs
from engine.risk import RiskContext
from engine.scenario import RiskInputError
from schemas.engine import CalculateRequest, Period

# 현재 서비스의 요청당 계산 보호 한도다. 통계적 최소 표본 수가 아니다.
MAX_RISK_FORECASTS = 8_000


@dataclass(frozen=True)
class RiskSamplingPolicy:
    version: str
    period_start: date
    period_end: date
    available_on: date
    origins: tuple[date, ...]
    min_samples: int

    def __post_init__(self):
        RiskContext((), self.period_start, self.period_end, self.min_samples)
        if (not isinstance(self.version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.version)
                or type(self.available_on) is not date or self.available_on <= self.period_end
                or not isinstance(self.origins, tuple) or not self.origins
                or any(type(d) is not date or not self.period_start <= d < self.period_end for d in self.origins)
                or self.origins != tuple(sorted(set(self.origins)))):
            raise RiskInputError("정책 버전·확정일·중복 없는 정렬된 조정 기준일을 명시해 주세요")


def prepare_risk_context(
    request: CalculateRequest, product_daily: pd.DataFrame, policy: RiskSamplingPolicy,
) -> RiskContext | None:
    """동일 source·기준일 이하 자료로 D=g+H별 TUNE batch를 요청마다 만든다.

    예측 호출 상한 = SKU 수 × 성숙한 기준일 수 × A/B2개를 D별 합산한 값이다.
    정책 확정 이전 snapshot에는 미래에 고정된 정책을 소급 적용하지 않는다.
    pool은 productIds에 제한하지 않으며 회원·창고 소유권 검증은 app/repository가 먼저 수행한다.
    """
    settings, daily, ids = _inputs(request, product_daily)
    if request.today < policy.available_on:
        return None
    gap = (request.today - request.as_of_date).days
    horizons = sorted({gap + settings[p].lead_time_days + settings[p].review_cycle_days
                       for p in ids if p in settings})
    cutoff = min(request.as_of_date, policy.period_end)
    # 조정 구간에 실적 행이 전혀 없는 작년 전용 SKU는 유효 비율을 만들 수 없다.
    # productIds로 pool을 줄이지 않으며, 기준일 이후 처음 나타난 상품도 넣지 않는다.
    tune_ids = list(daily.loc[
        (daily["stock_date"] >= policy.period_start) & (daily["stock_date"] <= cutoff), "product_id"
    ].unique())
    origins_by_horizon = {}
    for horizon in horizons:
        # 거대한 gap은 기존 실행 용량 제한에서 거부한다. 날짜 덧셈 overflow도 요청 오류다.
        try:
            origins_by_horizon[horizon] = tuple(o for o in policy.origins if o + timedelta(days=horizon) <= cutoff)
        except OverflowError as exc:
            raise CalculationInputError("위험 평가 기간이 날짜 범위를 넘어요") from exc
    calls = len(tune_ids) * 2 * sum(len(origins) for origins in origins_by_horizon.values())
    if calls > MAX_RISK_FORECASTS:
        raise CalculationInputError("위험 표본 생성의 요청당 계산 지원 범위를 초과했어요")
    batches = []
    try:
        for horizon, origins in origins_by_horizon.items():
            if not origins or not tune_ids:
                continue
            batches.append(generate_error_samples(
                daily, origins=origins, horizon_days=horizon,
                period=Period(start=policy.period_start, end=policy.period_end),
                period_name=SamplePeriod.TUNE, observed_through=request.as_of_date,
                product_ids=tune_ids,
            ))
    except (BacktestInputError, RiskInputError) as exc:
        raise CalculationInputError("위험 표본의 원본·기간·산식 입력을 확인해 주세요") from exc
    return RiskContext(tuple(batches), policy.period_start, policy.period_end, policy.min_samples)
