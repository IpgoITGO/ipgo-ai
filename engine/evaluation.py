"""DA-08~13 TUNE 정책 고정 후 TEST 관측 출고 재생. 외부 I/O 없음."""
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from engine import ENGINE_VERSION
from engine.accuracy import empirical_range, grade
from engine.backtest import BacktestInputError, ErrorSample, SamplePeriod, generate_error_sample_batches
from engine.forecast import forecast_a, forecast_b
from engine.replenish import required_qty, round_to_order_unit
from engine.risk import RiskContext, select_risk_samples
from engine.scenario import simulate, stockout_risk
from schemas.base import AccuracyGrade, Method
from schemas.engine import (
    AccuracyHistoryPoint,
    ActualInboundComparison,
    BacktestRequest,
    BacktestResponse,
    MethodError,
    MultiplierMetric,
    RiskBucket,
    ScenarioReliability,
    SkuAccuracy,
)

MAX_BACKTEST_FORECASTS = 16_000
MIN_SAMPLES = 20


def errors(samples):
    """MAE=Σ|e|/N(개), WAPE=Σ|e|/Σ실제(비율). 미관측/분모0은 None."""
    total = math.fsum(s.actual_qty for s in samples)
    absolute = math.fsum(s.absolute_error for s in samples)
    return (absolute / len(samples) if samples else None, absolute / total if total else None)


def choose_method(samples):
    """동일 SKU·기준일·H의 A/B만 비교한다. A WAPE≤.9×B이면 A, 나머지는 B."""
    by_method = {m: {(s.product_id, s.origin_date, s.horizon_days): s for s in samples if s.method == m}
                 for m in Method}
    common = by_method[Method.A].keys() & by_method[Method.B].keys()
    a = errors([by_method[Method.A][k] for k in common])[1]
    b = errors([by_method[Method.B][k] for k in common])[1]
    if a is None or b is None:
        return Method.B, "A/B 공통 비교 실적이 없어 기존 기본 B 유지"
    selected = Method.A if a <= .9 * b else Method.B
    return selected, f"TUNE A/B 공통 {len(common)}건의 WAPE에서 A가 B의90% 이하이면 A, 그 외 B"


def choose_multiplier(metrics):
    """커버≥95%인 최소 배수. 없으면 최고 커버/최소 배수, 미측정이면 None."""
    measured = [m for m in metrics if m.cover_success_rate is not None]
    qualified = [m for m in measured if m.meets_target]
    if qualified:
        return min(qualified, key=lambda m: m.multiplier)
    return min(measured, key=lambda m: (-m.cover_success_rate, m.multiplier)) if measured else None


@dataclass(frozen=True)
class Case:
    sample: ErrorSample
    stock: float
    demand: tuple[float, ...]
    inbound: tuple[float, ...]
    adjustment: tuple[float, ...]


def case_from_sample(sample, indexed):
    """완전한 유효 실적에 더해 시작 재고·실제 입고/조정·일별 재고 항등식을 확인한다(개)."""
    rows = indexed.get(sample.product_id)
    if rows is None:
        return None
    days = [sample.origin_date + timedelta(days=k) for k in range(sample.horizon_days + 1)]
    if any(d not in rows for d in days):
        return None
    try:
        selected = [rows[d] for d in days]
        if any(len(v) != 1 for v in selected):
            return None
        selected = [v[0] for v in selected]
        stock = float(selected[0].closing_qty)
        if not math.isfinite(stock) or stock < 0:
            return None
        demand, inbound, adjustment = [], [], []
        prior = stock
        for row in selected[1:]:
            opening, inc, out, adj, closing = map(float, (
                row.opening_qty, row.inbound_qty, row.outbound_qty, row.adjustment_qty, row.closing_qty))
            if (not all(math.isfinite(q) for q in (opening, inc, out, adj, closing))
                    or min(opening, inc, out, closing) < 0
                    or not math.isclose(opening, prior, abs_tol=1e-9)
                    or not math.isclose(opening + inc - out + adj, closing, abs_tol=1e-9)
                    or out > opening + inc):
                return None
            demand.append(out)
            inbound.append(inc)
            adjustment.append(adj)
            prior = closing
        return Case(sample, stock, tuple(demand), tuple(inbound), tuple(adjustment))
    except (AttributeError, ValueError, TypeError, OverflowError):
        return None


def index_daily(daily):
    indexed = defaultdict(lambda: defaultdict(list))
    for row in daily.itertuples(index=False):
        indexed[int(row.product_id)][row.stock_date].append(row)
    return indexed


def order_for(case, setting, multiplier):
    """예측 일평균→신규 입고0의 도착 전 손실→필요량→입고단위 올림(개). 미래 실적 미사용."""
    forecast = case.sample.forecast_qty
    daily = forecast / case.sample.horizon_days
    baseline = simulate(case.stock, [daily] * case.sample.horizon_days, 0, setting.lead_time_days, {})
    return round_to_order_unit(required_qty(forecast, multiplier, case.stock, 0, baseline.pre_arrival_unmet),
                               setting.order_unit_qty)


@dataclass(frozen=True)
class Replay:
    closing: tuple[float, ...]
    unmet: tuple[float, ...]


def replay(case, order, lead, *, actual_inbound=False):
    """동일한 관측 출고·조정 재생. 입고→출고→조정, 음수 조정은 가용 재고까지만 반영(개)."""
    stock, closing, unmet = case.stock, [], []
    for k, (demand, inbound, adjustment) in enumerate(zip(case.demand, case.inbound, case.adjustment, strict=True), 1):
        stock += inbound if actual_inbound else (order if k == lead else 0)
        shipped = min(stock, demand)
        stock = max(stock - shipped + adjustment, 0.)
        closing.append(stock)
        unmet.append(demand - shipped)
    return Replay(tuple(closing), tuple(unmet))


def policy_cases(samples, selected_method, settings, indexed):
    grouped = defaultdict(dict)
    for sample in samples:
        # 설정된 상품의 자기 H만 사용한다. 다른 SKU 설정의 H 표본을 섞지 않는다.
        setting = settings.get(sample.product_id)
        if setting and sample.horizon_days == setting.lead_time_days + setting.review_cycle_days:
            grouped[(sample.product_id, sample.origin_date)][sample.method] = sample
    cases, skipped = [], 0
    alternate = Method.B if selected_method == Method.A else Method.A
    for key in sorted(grouped):
        methods = grouped[key]
        sample = methods.get(selected_method, methods.get(alternate))
        case = case_from_sample(sample, indexed)
        if case is None:
            skipped += 1
        else:
            cases.append(case)
    return cases, skipped


def metrics_for(cases, settings, multipliers):
    metrics = []
    for multiplier in multipliers:
        success = excess = 0
        for case in cases:
            cfg = settings[case.sample.product_id]
            actual = replay(case, order_for(case, cfg, multiplier), cfg.lead_time_days)
            success += all(q > 0 for q in actual.closing)
            excess += actual.closing[-1] > .5 * case.sample.actual_qty
        rate = success / len(cases) if cases else None
        metrics.append(MultiplierMetric(multiplier=multiplier, cover_success_rate=rate,
                                        excess_rate=excess / len(cases) if cases else None,
                                        meets_target=rate is not None and rate >= .95))
    return metrics


def accuracy_for(product_id, valid_days, cases, setting, multiplier, method, horizon, context, cutoff, forecast=None):
    """동일 SKU·실제 산식·H의 TUNE 커버/WAPE, LOW 경험 범위. 단위 개·일·비율."""
    selected = [c for c in cases if c.sample.product_id == product_id and c.sample.method == method
                and c.sample.horizon_days == horizon]
    history = []
    for case in selected:
        actual = replay(case, order_for(case, setting, multiplier), setting.lead_time_days)
        history.append(AccuracyHistoryPoint(origin_date=case.sample.origin_date, 
                                            forecast_qty=case.sample.forecast_qty,
                                            actual_qty=case.sample.actual_qty,
                                            covered=all(q > 0 for q in actual.closing)))
    count = len(history)
    success = sum(h.covered for h in history)
    level = grade(valid_days, success / count if count else None)
    low = high = None
    if level == AccuracyGrade.LOW and forecast is not None and context is not None:
        selection = select_risk_samples(context, product_id=product_id, method=method,
                                        horizon_days=horizon, observed_through=cutoff)
        if selection.basis is not None:
            low, high = empirical_range(forecast, selection.ratios)
    return SkuAccuracy(product_id=product_id, grade=level, valid_observation_days=valid_days,
                       cover_success_count=success if count else None, cover_trial_count=count or None,
                       forecast_error_rate=errors([c.sample for c in selected])[1],
                       range_low=low, range_high=high, history=history)


def evaluate(request: BacktestRequest, product_daily: pd.DataFrame) -> BacktestResponse:
    """5~7월로 고정한 정책의 8~9월 기준일/10월14일 마감 오프라인 재생. TEST로 재선정하지 않는다."""
    from engine.calculate import CalculationInputError
    try:
        return _evaluate(request, product_daily)
    except (BacktestInputError, ValueError, TypeError, OverflowError, KeyError) as exc:
        raise CalculationInputError("백테스트 기간·상품 설정·원본을 확인해 주세요") from exc


def _evaluate(request, daily):
    year = request.tune_period.start.year
    tune, test = request.tune_period, request.test_period
    if ((tune.start, tune.end) != (date(year, 5, 1), date(year, 7, 31))
            or (test.start, test.end) != (date(year, 8, 1), date(year, 10, 14))):
        raise BacktestInputError("TUNE5/1~7/31, TEST8/1~10/14를 명시해 주세요")
    settings = {s.product_id: s for s in request.settings}
    if not settings or len(settings) != len(request.settings) or any(p < 1 for p in settings):
        raise BacktestInputError("중복 없는 상품 설정이 필요해요")
    multipliers = request.multiplier_candidates
    if not multipliers or len(set(multipliers)) != len(multipliers) or any(
        isinstance(m, bool) or not math.isfinite(m) or not 1 <= m <= 1.5 for m in multipliers
    ):
        raise BacktestInputError("중복 없는 유한한 배수1~1.5가 필요해요")
    multipliers = sorted(multipliers)
    required = {"product_id", "upload_file_id", "stock_date", "opening_qty", "inbound_qty", "outbound_qty",
                "adjustment_qty", "closing_qty", "is_valid_observation"}
    if not required <= set(daily.columns) or not daily.columns.is_unique:
        raise BacktestInputError("평가에 필요한 일별 기록 열이 없어요")
    daily = daily[daily.upload_file_id.isin(request.upload_file_ids)].copy()
    if not daily.stock_date.map(lambda d: type(d) is date).all():
        raise BacktestInputError("날짜 형식이 잘못됐어요")
    daily = daily[daily.stock_date <= test.end]
    horizons = sorted({s.lead_time_days + s.review_cycle_days for s in settings.values()})
    plans = []
    for phase, period, origin_end in [(SamplePeriod.TUNE, tune, tune.end),
                                      (SamplePeriod.TEST, test, date(year, 9, 30))]:
        for h in horizons:
            origins = tuple(period.start + timedelta(days=k) for k in range(0, (origin_end-period.start).days+1, 7)
                            if period.start + timedelta(days=k+h) <= period.end)
            plans.append((phase, period, h, origins))
    if daily.product_id.nunique() * 2 * sum(len(p[3]) for p in plans) > MAX_BACKTEST_FORECASTS:
        raise BacktestInputError("백테스트 요청당 계산 지원 범위 초과")
    batches = generate_error_sample_batches(daily, [dict(origins=o, horizon_days=h, period=p, period_name=phase,
        observed_through=test.end, product_ids=sorted(daily.product_id.unique())) for phase, p, h, o in plans])
    samples = [s for b in batches for s in b.samples if
               s.horizon_days == settings[s.product_id].lead_time_days + settings[s.product_id].review_cycle_days]
    tune_samples = [s for s in samples if s.period == SamplePeriod.TUNE]
    test_samples = [s for s in samples if s.period == SamplePeriod.TEST]
    method, reason = choose_method(tune_samples)
    indexed = index_daily(daily[daily.stock_date >= tune.start])
    tune_cases, tune_bad = policy_cases(tune_samples, method, settings, indexed)
    test_cases, test_bad = policy_cases(test_samples, method, settings, indexed)
    metrics = metrics_for(tune_cases, settings, multipliers)
    selected = choose_multiplier(metrics)
    context = RiskContext(tuple(b for b in batches if b.period == SamplePeriod.TUNE), tune.start, tune.end, MIN_SAMPLES)
    accuracy = []
    comparison = defaultdict(list)
    ending_errors, bucket_values = [], [[], [], []]
    selections = {}
    for product_id, cfg in sorted(settings.items()):
        rows = daily[(daily.product_id == product_id) & (daily.stock_date <= tune.end)]
        # 중복 날짜를 유효 관측일로 이중 집계하지 않는다.
        valid = rows.drop_duplicates("stock_date").is_valid_observation
        if not valid.map(lambda v: type(v) is bool).all() or rows.stock_date.duplicated().any():
            valid_days = 0
        else:
            valid_days = int(valid.sum())
        h = cfg.lead_time_days + cfg.review_cycle_days
        forecast = {Method.A: forecast_a, Method.B: forecast_b}[method](rows, tune.end, h)
        used_method = method
        if forecast.qty is None:
            used_method = Method.B if method == Method.A else Method.A
            forecast = {Method.A: forecast_a, Method.B: forecast_b}[used_method](rows, tune.end, h)
        accuracy.append(accuracy_for(product_id, valid_days, tune_cases, cfg, selected.multiplier if selected else 1.,
                                     used_method, h, context, tune.end, forecast.qty))
    if selected is not None:
        for case in test_cases:
            s = case.sample
            cfg = settings[s.product_id]
            qty = order_for(case, cfg, selected.multiplier)
            observed = replay(case, qty, cfg.lead_time_days)
            actual = replay(case, 0, cfg.lead_time_days, actual_inbound=True)
            comparison[s.product_id].append((observed, actual))
            key = (s.product_id, s.method, s.horizon_days)
            if key not in selections:
                selections[key] = select_risk_samples(context, product_id=s.product_id, method=s.method,
                                                       horizon_days=s.horizon_days, observed_through=tune.end)
            selection = selections[key]
            historical = daily[(daily.product_id == s.product_id) & (daily.stock_date <= s.origin_date)]
            orders = sorted({round_to_order_unit(qty * percent / 100, cfg.order_unit_qty)
                             for percent in (80, 100, 120)})
            for scenario_qty in orders:
                predicted = simulate(case.stock, [s.forecast_qty/s.horizon_days] * s.horizon_days,
                                     scenario_qty, cfg.lead_time_days, {})
                replayed = replay(case, scenario_qty, cfg.lead_time_days)
                ending_errors.append(abs(predicted.ending_stock - replayed.closing[-1]))
                if selection.basis is None or int(historical.is_valid_observation.sum()) < 14:
                    continue
                risk = stockout_risk(selection.ratios, MIN_SAMPLES, as_of_closing=case.stock,
                                     daily_forecast=s.forecast_qty/s.horizon_days, gap_days=0,
                                     horizon_days=s.horizon_days, order_qty=scenario_qty,
                                     lead_time_days=cfg.lead_time_days, gap_receipts={}, planned_receipts={})
                p = risk.post_arrival_risk
                bucket_values[0 if p < .1 else 1 if p < .3 else 2].append(
                    any(q > 0 for q in replayed.unmet[cfg.lead_time_days-1:]))
    inbound = []
    for product_id, values in sorted(comparison.items()):
        engine = [q for r, _ in values for q in r.closing]
        actual = [q for _, r in values for q in r.closing]
        inbound.append(ActualInboundComparison(product_id=product_id,
            engine_shortage_days=sum(q <= 0 for q in engine), engine_average_stock=math.fsum(engine)/len(engine),
            actual_shortage_days=sum(q <= 0 for q in actual), actual_average_stock=math.fsum(actual)/len(actual)))
    skipped = Counter(f"{b.period}:{s.reason}" for b in batches for s in b.skipped)
    skipped.update({"TUNE:INVALID_REPLAY": tune_bad, "TEST:INVALID_REPLAY": test_bad})
    buckets = [RiskBucket(low=lo, high=hi, cases=len(values),
                          actual_stockout_rate=sum(values)/len(values) if len(values) >= MIN_SAMPLES else None)
               for (lo, hi), values in zip(((0,.1),(.1,.3),(.3,1)), bucket_values, strict=True)]
    test_metric = metrics_for(test_cases, settings, [selected.multiplier])[0] if selected else None
    return BacktestResponse(engine_version=ENGINE_VERSION, selected_method=method,
        selected_multiplier=selected.multiplier if selected else None, selection_reason=reason + "; " + (
            "커버95% 달성 최소 배수" if selected and selected.meets_target else
            "커버 목표 미달: 최고 성공률/동률 최소 배수" if selected else "유효 재생 사례가 없어 배수 미선정"),
        target_met=bool(selected and selected.meets_target),
        method_errors=[MethodError(period=phase.value, method=m, mae=errors(ss)[0], wape=errors(ss)[1],
                                   sample_size=len(ss))
                       for phase in SamplePeriod for m in Method
                       for ss in [[s for s in samples if s.period == phase and s.method == m]]],
        multiplier_metrics=metrics, actual_inbound_comparison=inbound,
        scenario_reliability=ScenarioReliability(ending_stock_mae=math.fsum(ending_errors)/len(ending_errors)
                                                if ending_errors else None, risk_buckets=buckets,
                                                scenario_cases=len(ending_errors)),
        test_policy_metric=test_metric,
        excess_target_met=selected.excess_rate <= .1 if selected and selected.excess_rate is not None else None,
        sku_accuracy=accuracy, skipped_samples=dict(sorted(skipped.items())), evaluation_notes=[
            "관측 출고 평가: 잠재 수요·독립 표본·신뢰구간이 아님",
            "TUNE 정책 고정 후 TEST 평가; 주간 창 중복 포함",
            "DA12 과거 예정 발주 정보 없음: 계산 경로에 미래 실제 입고 미사용",
            "관측 재생은 동일 실제 조정 적용, 음수 조정은 가용 재고까지만; 미래 예측에 조정 미사용",
            "DA12 부족일/평균재고는 TEST 평가 창의 일자 노출 합계(중복 창 포함)",
            "DA10 각 위험 구간20건 미만 경험 비율 미산출",
            "DA10 P80/P100/P120의 고유 입고 수량만 집계(같은 수량 중복 제외), 사례 간 독립성 보장 없음",
            "targetMet은 TUNE 커버95%만 의미하며, 과잉10% 목표는 excessTargetMet으로 별도 보고",
            "SKU 정확도는 TUNE 말일 기준; 커버 실적이 없으면 HIGH 미부여",
        ])
