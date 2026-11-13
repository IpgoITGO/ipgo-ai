"""DA-07: DA08 조정 표본을 실제 산식·예측 길이별로 선택한다. 정책값은 호출자가 명시한다."""
import math
from dataclasses import dataclass
from datetime import date, timedelta

from engine.backtest import ErrorSample, ErrorSampleBatch, SamplePeriod
from engine.scenario import RiskInputError
from schemas.base import Method, RiskBasis


@dataclass(frozen=True)
class RiskContext:
    """같은 회원·창고·승인한 업로드의 DA08 표본과 명시한 5~7월 조정 정책.

    최소 표본 기준·기준일 간격은 미확정이므로 기본값이나 자동 생성이 없다.
    서로 다른 예측 길이의 batch를 전달할 수 있지만 같은 표본을 중복 공급할 수 없다.
    """

    batches: tuple[ErrorSampleBatch, ...]
    period_start: date
    period_end: date
    min_samples: int
    policy_version: str | None = None
    policy_hash: str | None = None
    # 계산량 보호 한도로 생성하지 않은 길이. 실제 표본 부족과 구분한다.
    unavailable_horizons: tuple[int, ...] = ()

    def __post_init__(self):
        if (type(self.period_start) is not date or type(self.period_end) is not date
                or self.period_start != date(self.period_start.year, 5, 1)
                or self.period_end != date(self.period_start.year, 7, 31)
                or type(self.min_samples) is not int or self.min_samples < 1
                or not isinstance(self.unavailable_horizons, tuple)
                or any(type(h) is not int or h < 1 for h in self.unavailable_horizons)
                or self.unavailable_horizons != tuple(sorted(set(self.unavailable_horizons)))
                or not isinstance(self.batches, tuple)
                or any(not isinstance(b, ErrorSampleBatch) or not isinstance(b.samples, tuple)
                       or any(not isinstance(s, ErrorSample) for s in b.samples) for b in self.batches)):
            raise RiskInputError("5/1~7/31 조정 기간·양의 최소 표본 수·DA08 batch tuple을 명시해 주세요")


@dataclass(frozen=True)
class ExcludedRiskSample:
    sample: ErrorSample
    reason: str


@dataclass(frozen=True)
class RiskSelection:
    samples: tuple[ErrorSample, ...]
    basis: RiskBasis | None
    excluded: tuple[ExcludedRiskSample, ...]

    @property
    def ratios(self) -> tuple[float, ...]:
        return tuple(s.actual_to_forecast_ratio for s in self.samples)


def _ratio_valid(sample: ErrorSample) -> bool:
    """예측0/overflow는 제외한다. 모순된 수량·비율을 표본 부족으로 숨기지 않는다."""
    quantities = (sample.forecast_qty, sample.actual_qty)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
           for v in quantities):
        raise RiskInputError("DA08 표본의 예측·실적 수량이 잘못됐어요")
    if (isinstance(sample.error_qty, bool) or not isinstance(sample.error_qty, (int, float))
            or not math.isclose(sample.error_qty, sample.actual_qty - sample.forecast_qty,
                                rel_tol=1e-12, abs_tol=1e-12)):
        raise RiskInputError("DA08 표본의 오차 수량이 일치하지 않아요")
    ratio = sample.actual_to_forecast_ratio
    if sample.forecast_qty == 0:
        if ratio is not None or sample.ratio_unavailable_reason != "ZERO_FORECAST":
            raise RiskInputError("예측0의 비율·제외 사유가 모순돼요")
        return False
    expected = sample.actual_qty / sample.forecast_qty
    if not math.isfinite(expected):
        if ratio is not None or sample.ratio_unavailable_reason != "NONFINITE_RATIO":
            raise RiskInputError("비유한 비율의 제외 사유가 모순돼요")
        return False
    if (isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio < 0
            or sample.ratio_unavailable_reason is not None
            or not math.isclose(ratio, expected, rel_tol=1e-12, abs_tol=0)):
        raise RiskInputError("DA08 표본의 실적/예측 비율이 일치하지 않아요")
    return True


def select_risk_samples(
    context: RiskContext, *, product_id: int, method: Method, horizon_days: int, observed_through: date,
) -> RiskSelection:
    """동일한 실제 산식·D=g+H일 예측의 확정된 TUNE만 쓴다. SKU가 최소 수 이상이면 SKU, 다음은 전체 SKU.

    pooled는 대상 SKU도 포함한 동일 조건 전체 SKU다. 모두 부족하면 basis=None과 실제 후보 수를 반환한다.
    겹치는 평가 창도 유지하지만 독립 표본 수나 신뢰구간으로 해석하지 않는다.
    """
    _validate_selection(context, product_id, method, horizon_days, observed_through)
    eligible, excluded = _collect_candidates(context, method, horizon_days, observed_through)
    sku = tuple(s for s in eligible if s.product_id == product_id)
    if len(sku) >= context.min_samples:
        return RiskSelection(sku, RiskBasis.SKU, excluded)
    basis = RiskBasis.POOLED if len(eligible) >= context.min_samples else None
    return RiskSelection(eligible, basis, excluded)


def _validate_selection(context, product_id, method, horizon_days, observed_through):
    if (not isinstance(context, RiskContext) or type(product_id) is not int or product_id < 1
            or not isinstance(method, Method) or type(horizon_days) is not int or horizon_days < 1
            or type(observed_through) is not date):
        raise RiskInputError("상품·실제 산식·예측 일수·관측 확정일을 확인해 주세요")


def _collect_candidates(context, method, horizon_days, observed_through):
    eligible, excluded, seen = [], [], set()
    for batch in context.batches:
        if (type(batch.observed_through) is not date or type(batch.period_start) is not date
                or type(batch.period_end) is not date or batch.period_start > batch.period_end):
            raise RiskInputError("batch의 기간·관측 확정일이 잘못됐어요")
        for sample in batch.samples:
            reason = None
            if batch.period != SamplePeriod.TUNE or sample.period != SamplePeriod.TUNE:
                reason = "NOT_TUNE"
            elif (batch.period_start, batch.period_end) != (context.period_start, context.period_end):
                reason = "OTHER_TUNE_PERIOD"
            elif sample.method != method:
                reason = "OTHER_METHOD"
            elif sample.horizon_days != horizon_days:
                reason = "OTHER_HORIZON"
            elif (type(sample.origin_date) is not date or type(sample.target_end) is not date
                  or sample.target_end != sample.origin_date + timedelta(days=horizon_days)
                  or type(sample.product_id) is not int or sample.product_id < 1):
                raise RiskInputError("DA08 표본의 상품·기준일·종료일이 모순돼요")
            elif sample.origin_date < context.period_start or sample.target_end > context.period_end:
                reason = "OUTSIDE_TUNE"
            elif sample.target_end > min(observed_through, batch.observed_through):
                reason = "NOT_OBSERVED_YET"
            else:
                key = (sample.product_id, sample.method, sample.origin_date, sample.horizon_days)
                if key in seen:
                    raise RiskInputError("같은 DA08 오차 표본이 중복됐어요")
                seen.add(key)
                if not _ratio_valid(sample):
                    reason = sample.ratio_unavailable_reason
            if reason is not None:
                excluded.append(ExcludedRiskSample(sample, reason))
            else:
                eligible.append(sample)
    eligible.sort(key=lambda s: (s.product_id, s.origin_date))
    return tuple(eligible), tuple(excluded)


class _RiskSelector:
    """한 요청의 같은 context·산식·기간·관측일 후보를 처음 필요할 때 검사해 재사용한다.

    조건이 많아도 제외 목록을 무한 보관하지 않도록 최대 네 조건만 유지한다.
    """

    def __init__(self, context: RiskContext):
        self._context = context
        self._candidates = {}

    def select(self, *, product_id: int, method: Method, horizon_days: int,
               observed_through: date) -> RiskSelection:
        context = self._context
        _validate_selection(context, product_id, method, horizon_days, observed_through)
        key = (method, horizon_days, observed_through)
        if key not in self._candidates:
            pooled, excluded = _collect_candidates(context, method, horizon_days, observed_through)
            by_product = {}
            for sample in pooled:
                by_product.setdefault(sample.product_id, []).append(sample)
            if len(self._candidates) >= 4:
                del self._candidates[next(iter(self._candidates))]
            self._candidates[key] = (pooled, excluded, {p: tuple(rows) for p, rows in by_product.items()})
        pooled, excluded, by_product = self._candidates[key]
        sku = by_product.get(product_id, ())
        if len(sku) >= context.min_samples:
            return RiskSelection(sku, RiskBasis.SKU, excluded)
        basis = RiskBasis.POOLED if len(pooled) >= context.min_samples else None
        return RiskSelection(pooled, basis, excluded)
