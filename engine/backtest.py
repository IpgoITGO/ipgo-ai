"""DA-08 시계열 오차 표본. DA-09/10/12와 전체 응답 조립은 후속이다.

예측은 기준일 t까지, 실적은 (t, t+H]의 완전한 유효 관측만 사용한다.
오차는 입고 단위 올림 전 소수 수량이며 실제 수요가 아닌 관측 출고량과 비교한다.
"""
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from numbers import Integral

import pandas as pd

from engine.forecast import forecast_a, forecast_b
from schemas.base import Method
from schemas.engine import BacktestRequest, BacktestResponse, Period


class SamplePeriod(StrEnum):
    TUNE = "TUNE"
    TEST = "TEST"


class SkipReason(StrEnum):
    OUTSIDE_PERIOD = "OUTSIDE_PERIOD"
    NOT_OBSERVED_YET = "NOT_OBSERVED_YET"
    DUPLICATE_RECORDS = "DUPLICATE_RECORDS"
    INVALID_HISTORY = "INVALID_HISTORY"
    FORECAST_UNAVAILABLE = "FORECAST_UNAVAILABLE"
    INVALID_FORECAST = "INVALID_FORECAST"
    INCOMPLETE_ACTUALS = "INCOMPLETE_ACTUALS"
    UNOBSERVED_ACTUALS = "UNOBSERVED_ACTUALS"
    INVALID_ACTUALS = "INVALID_ACTUALS"


@dataclass(frozen=True)
class ErrorSample:
    product_id: int
    period: SamplePeriod
    method: Method
    origin_date: date
    horizon_days: int
    target_end: date
    forecast_qty: float
    actual_qty: float
    error_qty: float                     # 관측 출고−예측, 개
    actual_to_forecast_ratio: float | None
    ratio_unavailable_reason: str | None  # ZERO_FORECAST / NONFINITE_RATIO

    @property
    def absolute_error(self) -> float:
        return abs(self.error_qty)


@dataclass(frozen=True)
class SkippedSample:
    product_id: int
    period: SamplePeriod
    method: Method
    origin_date: date
    horizon_days: int
    target_end: date
    reason: SkipReason
    detail: str
    forecast_qty: float | None = None


@dataclass(frozen=True)
class ErrorSampleBatch:
    period: SamplePeriod
    period_start: date
    period_end: date
    observed_through: date
    samples: tuple[ErrorSample, ...]
    skipped: tuple[SkippedSample, ...]


class BacktestInputError(ValueError):
    """상품/날짜/기간/산식의 구조가 모순된 백테스트 입력."""


_COLUMNS = ["product_id", "stock_date", "outbound_qty", "is_valid_observation"]


def _dates(origins: Sequence[date]) -> list[date]:
    if any(type(day) is not date for day in origins) or len(set(origins)) != len(origins):
        raise BacktestInputError("기준일은 중복 없는 날짜여야 해요")
    return sorted(origins)


def _ids(values: Sequence[int]) -> list[int]:
    if (any(not isinstance(v, Integral) or isinstance(v, bool) or v <= 0 for v in values)
            or len(set(values)) != len(values)):
        raise BacktestInputError("상품 ID는 중복 없는 양의 정수여야 해요")
    return sorted(int(v) for v in values)


def _quantities(rows: pd.DataFrame) -> pd.DataFrame | None:
    """유효 관측의 출고량만 유한/비음수인지 검사한다. 제외일의 누락량을 0으로 보충하지 않는다."""
    if not rows["is_valid_observation"].map(lambda v: type(v) is bool).all():
        return None
    try:
        qty = pd.to_numeric(rows["outbound_qty"], errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    valid = rows["is_valid_observation"]
    measured = qty[valid]
    if (not measured.map(lambda v: pd.notna(v) and math.isfinite(v) and v >= 0).all()
            or rows.loc[valid, "outbound_qty"].map(lambda v: isinstance(v, bool)).any()):
        return None
    return rows.assign(outbound_qty=qty)


def _actual(rows: pd.DataFrame, horizon_days: int) -> tuple[float | None, SkipReason | None]:
    if rows["stock_date"].duplicated().any():
        return None, SkipReason.DUPLICATE_RECORDS
    if len(rows) != horizon_days:
        return None, SkipReason.INCOMPLETE_ACTUALS
    if not rows["is_valid_observation"].map(lambda v: type(v) is bool).all():
        return None, SkipReason.INVALID_ACTUALS
    if not rows["is_valid_observation"].all():
        return None, SkipReason.UNOBSERVED_ACTUALS
    normalized = _quantities(rows)
    if normalized is None:
        return None, SkipReason.INVALID_ACTUALS
    try:
        total = math.fsum(normalized["outbound_qty"])
    except OverflowError:
        return None, SkipReason.INVALID_ACTUALS
    return (total, None) if math.isfinite(total) else (None, SkipReason.INVALID_ACTUALS)


def _generate_error_samples(
    product_daily: pd.DataFrame, *, origins: Sequence[date], horizon_days: int,
    period: Period, period_name: SamplePeriod, observed_through: date,
    product_ids: Sequence[int] | None = None, methods: Sequence[Method] = (Method.A, Method.B),
    _history_cache: dict | None = None,
) -> ErrorSampleBatch:
    """명시한 기준일별 A/B를 검증한다. 기본 정책·대체 산식·위험도를 선택하지 않는다.

    구간은 origin >= start 및 origin+H <= end, 실적 확정은 origin+H <= observed_through다.
    error_qty = 관측 출고−예측(개), actual_to_forecast_ratio = 관측 출고÷예측(무차원). 예측0은 비율None.
    기준일 간격은 호출자가 정한다. 겹치는 평가 구간을 독립 표본으로 간주하거나 TUNE/TEST를 합산하지 않는다.
    """
    if (not isinstance(period, Period) or period.start > period.end or type(observed_through) is not date
            or type(horizon_days) is not int or horizon_days <= 0):
        raise BacktestInputError("기간·관측 확정일·양의 정수 커버 일수를 확인해 주세요")
    dates = _dates(origins)
    try:
        phase = SamplePeriod(period_name)
        selected_methods = sorted({Method(m) for m in methods}, key=lambda m: m.value)
    except (TypeError, ValueError) as exc:
        raise BacktestInputError("구간은 TUNE/TEST, 산식은 A/B여야 해요") from exc
    if not selected_methods or len(selected_methods) != len(methods):
        raise BacktestInputError("산식은 중복 없이 A/B를 하나 이상 지정해 주세요")
    if (not isinstance(product_daily, pd.DataFrame) or not product_daily.columns.is_unique
            or not set(_COLUMNS) <= set(product_daily.columns)):
        raise BacktestInputError("상품 일별 기록에 필요한 열이 없어요")
    daily = product_daily[_COLUMNS].copy(deep=True)
    ids = _ids(product_ids) if product_ids is not None else None
    if ids is not None:
        daily = daily[daily["product_id"].isin(ids)]
    if not daily["stock_date"].map(lambda v: type(v) is date).all():
        raise BacktestInputError("stock_date는 날짜여야 해요")
    daily = daily[daily["stock_date"] <= observed_through]
    if ids is None:
        ids = _ids(list(daily["product_id"].unique()))
    if not daily["product_id"].map(lambda v: isinstance(v, Integral) and not isinstance(v, bool)).all():
        raise BacktestInputError("상품 ID 열은 정수여야 해요")
    daily = daily.sort_values(["product_id", "stock_date"])
    samples, skipped = [], []
    for product_id in ids:
        sku = daily[daily["product_id"] == product_id]
        for origin in dates:
            try:
                end = origin + timedelta(days=horizon_days)
            except OverflowError as exc:
                raise BacktestInputError("평가 종료일이 날짜 범위를 넘어요") from exc
            context = dict(product_id=product_id, period=phase, origin_date=origin,
                           horizon_days=horizon_days, target_end=end)
            reason = (SkipReason.OUTSIDE_PERIOD if origin < period.start or end > period.end else
                      SkipReason.NOT_OBSERVED_YET if end > observed_through else None)
            history = None
            if reason is None:
                key = (product_id, origin, observed_through)
                if _history_cache is not None and key in _history_cache:
                    history, reason = _history_cache[key]
                else:
                    history = sku[sku["stock_date"] <= origin].copy(deep=True)
                    if history["stock_date"].duplicated().any():
                        reason = SkipReason.DUPLICATE_RECORDS
                    else:
                        history = _quantities(history)
                        if history is None:
                            reason = SkipReason.INVALID_HISTORY
                    if _history_cache is not None:
                        _history_cache[key] = (history, reason)
            if reason is not None:
                skipped.extend(SkippedSample(**context, method=m, reason=reason, detail=reason.value)
                               for m in selected_methods)
                continue
            target = sku[(sku["stock_date"] > origin) & (sku["stock_date"] <= end)]
            actual, actual_reason = _actual(target, horizon_days)
            for method in selected_methods:
                forecast = (forecast_a if method == Method.A else forecast_b)(history, origin, horizon_days)
                predicted = forecast.qty
                if predicted is None:
                    skipped.append(SkippedSample(**context, method=method, reason=SkipReason.FORECAST_UNAVAILABLE,
                                                 detail=forecast.reason or "예측 불가"))
                elif not math.isfinite(predicted) or predicted < 0:
                    skipped.append(SkippedSample(**context, method=method, reason=SkipReason.INVALID_FORECAST,
                                                 detail="예측값이 유한한 비음수가 아니에요"))
                elif actual_reason is not None:
                    skipped.append(SkippedSample(**context, method=method, reason=actual_reason,
                                                 detail=actual_reason.value, forecast_qty=predicted))
                else:
                    ratio = actual / predicted if predicted else None
                    ratio_reason = "ZERO_FORECAST" if predicted == 0 else None
                    if ratio is not None and not math.isfinite(ratio):
                        ratio, ratio_reason = None, "NONFINITE_RATIO"
                    samples.append(ErrorSample(**context, method=method, forecast_qty=predicted, actual_qty=actual,
                                               error_qty=actual - predicted, actual_to_forecast_ratio=ratio,
                                               ratio_unavailable_reason=ratio_reason))
    return ErrorSampleBatch(phase, period.start, period.end, observed_through, tuple(samples), tuple(skipped))


def generate_error_samples(
    product_daily: pd.DataFrame, *, origins: Sequence[date], horizon_days: int,
    period: Period, period_name: SamplePeriod, observed_through: date,
    product_ids: Sequence[int] | None = None, methods: Sequence[Method] = (Method.A, Method.B),
) -> ErrorSampleBatch:
    """단일 기간의 원본 첫 시도 표본. 기준일 prefix·완전 실적·A/B 분리 규칙은 내부 생성기와 같다."""
    return _generate_error_samples(product_daily, origins=origins, horizon_days=horizon_days, period=period,
        period_name=period_name, observed_through=observed_through, product_ids=product_ids, methods=methods)


def generate_error_sample_batches(product_daily: pd.DataFrame, plans: Sequence[dict]) -> tuple[ErrorSampleBatch, ...]:
    """여러 H/D 평가에서 동일 SKU·기준일의 이력 검증만 재사용한다. 결과·산식·표본 개수는 동일하다.

    캐시는 이 함수 안의 SKU 하나에만 존재하며 요청·상품 간 공유하거나 미래 이력을 전달하지 않는다.
    한 SKU를 끝내면 폐기하므로 전체 원장의 모든 prefix를 동시에 보관하지 않는다.
    """
    from dataclasses import replace
    if not isinstance(product_daily, pd.DataFrame) or not set(_COLUMNS) <= set(product_daily.columns):
        raise BacktestInputError("필요한 상품 일별 열이 없어요")
    # 독립 단일 생성과 같은 입력 검증을 먼저 수행한다. 빈 원본에도 기간 오류를 숨기지 않는다.
    templates = [_generate_error_samples(product_daily.iloc[:0], **{**p, "product_ids": []}) for p in plans]
    ids_by_plan = [_ids(p["product_ids"]) if p.get("product_ids") is not None
                   else _ids(list(product_daily.product_id.unique())) for p in plans]
    samples, skipped = [[] for _ in plans], [[] for _ in plans]
    ids = sorted({p for group in ids_by_plan for p in group})
    for product_id in ids:
        sku = product_daily[product_daily.product_id == product_id]
        history_cache = {}
        for i, plan in enumerate(plans):
            if product_id not in ids_by_plan[i]:
                continue
            batch = _generate_error_samples(sku, **{**plan, "product_ids": [product_id]},
                                             _history_cache=history_cache)
            samples[i].extend(batch.samples)
            skipped[i].extend(batch.skipped)
    return tuple(replace(t, samples=tuple(samples[i]), skipped=tuple(skipped[i])) for i, t in enumerate(templates))


def run_backtest(request: BacktestRequest, product_daily: pd.DataFrame) -> BacktestResponse:
    """DA09/10/12/13을 포함한 전체 응답. 최소 표본 생성 성공으로 정책/등급을 채워 넣지 않는다."""
    from engine.evaluation import evaluate
    return evaluate(request, product_daily)
