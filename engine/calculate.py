"""DA-11: 예측 → 수량 → 시나리오 조립. 명시한 DA07 context가 있을 때 위험도 연결한다."""
from datetime import date, timedelta

import pandas as pd

from engine import ENGINE_VERSION
from engine.accuracy import empirical_range, grade
from engine.forecast import forecast_a, forecast_b
from engine.insight_facts import to_insight_input
from engine.replenish import (
    required_qty,
    round_to_order_unit,
    split_forecast,
    split_planned_receipts,
    start_stock,
    stockout_since,
)
from engine.risk import RiskContext, _RiskSelector, select_risk_samples
from engine.scenario import simulate, stockout_risk
from schemas.base import Method, ResultStatus, RiskStatus, ScenarioType
from schemas.engine import (
    CalculateRequest,
    CalculateResponse,
    DailyProjection,
    ProductResult,
    ProductSettings,
    ScenarioRequest,
    ScenarioResult,
)

DAILY_COLUMNS = {
    "product_id", "upload_file_id", "stock_date", "outbound_qty", "closing_qty", "is_valid_observation",
}


class CalculationInputError(ValueError):
    """계산 입력 자체가 모순돼 실행할 수 없음. 상품의 관측일 부족과 구분한다."""


def _inputs(request: CalculateRequest, product_daily: pd.DataFrame):
    if request.today < request.as_of_date:
        raise CalculationInputError("today는 asOfDate보다 빠를 수 없어요")
    settings = {s.product_id: s for s in request.settings}
    if len(settings) != len(request.settings):
        raise CalculationInputError("같은 상품의 입고 설정이 중복됐어요")
    if request.product_ids is not None and len(set(request.product_ids)) != len(request.product_ids):
        raise CalculationInputError("productIds에 같은 상품이 중복됐어요")

    # 조회 결과가 없어도 요청한 상품은 계산 불가 사유와 함께 반환한다.
    daily = product_daily if not product_daily.empty else pd.DataFrame(columns=sorted(DAILY_COLUMNS))
    if not DAILY_COLUMNS.issubset(daily.columns):
        raise CalculationInputError("상품 일별 기록에 계산에 필요한 열이 없어요")
    daily = daily[daily["upload_file_id"].isin(request.upload_file_ids)]
    if not daily["stock_date"].map(lambda value: type(value) is date).all():
        raise CalculationInputError("상품 일별 기록의 stock_date는 날짜여야 해요")
    daily = daily[daily["stock_date"] <= request.as_of_date]
    ids = request.product_ids
    if ids is None:
        ids = sorted(set(settings) | set(daily["product_id"]))
    return settings, daily, ids


def _not_calculable(product_id: int, reason: str, valid_days: int | None = None):
    return ProductResult(
        product_id=product_id, status=ResultStatus.NOT_CALCULABLE, reason=reason,
        valid_observation_days=valid_days,
    ), {}


def _product(
    request: CalculateRequest, daily: pd.DataFrame, product_id: int, settings: ProductSettings | None,
) -> tuple[ProductResult, dict[int, float]]:
    # 사유는 화면 · 인사이트 고정 문장에 그대로 보이므로 셀러가 할 일을 알 수 있는 말로 쓴다 (#68).
    # Spring은 입고 조건이나 원가가 비면 settings를 보내지 않는다.
    if settings is None:
        return _not_calculable(product_id, "입고 조건이나 원가가 비어 있어요. 입고 조건 화면에서 확인해 주세요")
    daily = daily[daily["product_id"] == product_id]
    if daily.empty:
        return _not_calculable(product_id, "기준일까지 이 상품의 수불부 기록이 없어요", 0)
    if daily["stock_date"].duplicated().any():
        return _not_calculable(product_id, "같은 날짜 기록이 두 번 있어요. 겹친 수불부를 확인해 주세요")
    quantities = daily[["outbound_qty", "closing_qty"]].apply(pd.to_numeric, errors="coerce")
    if quantities.isna().any().any() or not ((quantities >= 0) & (quantities < float("inf"))).all().all():
        return _not_calculable(product_id, "출고나 재고에 음수 · 빈 값이 있어요. 수불부를 확인해 주세요")
    if not daily["is_valid_observation"].map(lambda value: isinstance(value, bool)).all():
        return _not_calculable(product_id, "계산 준비가 덜 된 기록이 있어요. 다시 계산해 주세요")
    daily = daily.assign(**{column: quantities[column] for column in quantities})
    valid_days = int(daily["is_valid_observation"].sum())
    closing = daily.loc[daily["stock_date"] == request.as_of_date, "closing_qty"]
    if closing.empty:
        return _not_calculable(
            product_id, "기준일 재고가 비어 있어요. 수불부 마지막 날 재고를 확인해 주세요", valid_days,
        )

    horizon = settings.lead_time_days + settings.review_cycle_days
    gap = (request.today - request.as_of_date).days
    method = request.policy.default_method
    forecasts = {Method.A: forecast_a, Method.B: forecast_b}
    forecast = forecasts[method](daily, request.as_of_date, gap + horizon)
    method_reason = "요청 정책의 기본 방식"
    if forecast.qty is None:
        first_reason = forecast.reason
        alternate = Method.B if method is Method.A else Method.A
        forecast = forecasts[alternate](daily, request.as_of_date, gap + horizon)
        if forecast.qty is None:
            # 계산식 기호(A · B) 없이 두 사유를 문장으로 잇는다. 끝 마침표는 붙이지 않는다(인사이트 {{reason}}).
            return _not_calculable(product_id, f"{first_reason}. {forecast.reason}", valid_days)
        method_reason = f"기본 방식 {method.value} 사용 불가({first_reason})로 {alternate.value} 사용"
        method = alternate

    daily_forecast, cover = split_forecast(forecast.qty, gap, horizon)
    before, planned = split_planned_receipts(
        [(r.expected_on, r.quantity) for r in request.planned_receipts if r.product_id == product_id],
        request.as_of_date, request.today, horizon,
    )
    stock = start_stock(float(closing.iloc[0]), daily_forecast, gap, before)
    baseline = simulate(stock, [daily_forecast] * horizon, 0, settings.lead_time_days, planned)
    planned_qty = sum(planned.values())
    need = required_qty(cover, request.policy.multiplier, stock, planned_qty, baseline.pre_arrival_unmet)
    qty = round_to_order_unit(need, settings.order_unit_qty)

    # Reuse the tested no-new-order simulation (existing receipts still apply).
    # This is the first zero closing stock within this calculation's coverage window.
    # 오늘 시작 재고가 이미 0이면 "오늘+1"이 아니라 기준일부터 따라가 바닥난 날(오늘 이하)을 준다(#72).
    past = None
    if stock <= 0:
        past = stockout_since(request.as_of_date, float(closing.iloc[0]), daily_forecast, gap, before)
    future = request.today + timedelta(days=baseline.depletion_day) if baseline.depletion_day else None
    shortage = (past or future) if qty else None
    # DA07은 명시한 context가 있을 때 후속 조립한다. DA13 등급은 별도다.
    return ProductResult(
        product_id=product_id, status=ResultStatus.OK if qty else ResultStatus.NOT_NEEDED,
        method=method, method_reason=method_reason, method_fallback=method != request.policy.default_method,
        multiplier=request.policy.multiplier,
        horizon_days=horizon, forecast_outbound_qty=cover, start_stock_qty=stock, start_stock_estimated=gap > 0,
        planned_receipts_in_horizon=planned_qty, pre_arrival_unmet_qty=baseline.pre_arrival_unmet,
        required_qty_before_rounding=need, recommended_qty=qty, recommended_amount=qty * settings.unit_cost,
        valid_observation_days=valid_days, accuracy_grade=grade(valid_days, None), expected_shortage_on=shortage,
    ), planned


def _scenario(
    result: ProductResult, settings: ProductSettings, today: date, planned: dict[int, float],
    qty: int, kind: ScenarioType, duplicate: bool = False,
) -> ScenarioResult:
    """s+1〜s+H의 재고(개)를 시뮬레이션하고 상대 일수에 실제 날짜를 붙인다."""
    simulation = simulate(
        result.start_stock_qty, [result.forecast_outbound_qty / result.horizon_days] * result.horizon_days,
        qty, settings.lead_time_days, planned,
    )
    depletion = simulation.depletion_day
    return ScenarioResult(
        type=kind, qty=qty, is_duplicate=duplicate,
        expected_depletion_on=today + timedelta(days=depletion) if depletion is not None else None,
        expected_ending_stock=simulation.ending_stock, unmet_demand_qty=simulation.unmet_demand,
        daily_projection=[
            DailyProjection(date=today + timedelta(days=k), closing_qty=closing)
            for k, closing in enumerate(simulation.daily_closing, 1)
        ],
    )


def _apply_risk(
    result: ProductResult, request: CalculateRequest, daily: pd.DataFrame, settings: ProductSettings,
    planned: dict[int, float], context: RiskContext | None, selector: _RiskSelector | None = None,
) -> None:
    """동일한 표본으로 도착 전 공통·도착 후 시나리오별 위험을 채운다. 원래 수량/재고는 바꾸지 않는다."""
    if context is None or result.status not in (ResultStatus.OK, ResultStatus.NOT_NEEDED):
        return
    gap = (request.today - request.as_of_date).days
    choose = selector.select if selector is not None else lambda **kwargs: select_risk_samples(context, **kwargs)
    selection = choose(
        product_id=result.product_id, method=result.method,
        horizon_days=gap + result.horizon_days, observed_through=request.as_of_date,
    )
    result.risk_sample_size = len(selection.samples)
    # DA07의 확정된 7~13일 위험 억제만 적용한다. DA13 전체 등급/LOW 범위를 만들지 않는다.
    if 7 <= result.valid_observation_days <= 13:
        result.risk_status = RiskStatus.LOW_GRADE
        return
    if selection.basis is None:
        result.risk_status = RiskStatus.INSUFFICIENT_SAMPLE
        return
    closing = daily.loc[
        (daily["product_id"] == result.product_id) & (daily["stock_date"] == request.as_of_date), "closing_qty"
    ].iloc[0]
    before, _ = split_planned_receipts(
        [(r.expected_on, r.quantity) for r in request.planned_receipts if r.product_id == result.product_id],
        request.as_of_date, request.today, result.horizon_days,
    )
    for scenario in result.scenarios or [None]:
        estimate = stockout_risk(
            selection.ratios, context.min_samples, as_of_closing=float(closing),
            daily_forecast=result.forecast_outbound_qty / result.horizon_days, gap_days=gap,
            horizon_days=result.horizon_days, order_qty=scenario.qty if scenario else 0,
            lead_time_days=settings.lead_time_days, gap_receipts=before, planned_receipts=planned,
        )
        result.pre_arrival_risk = estimate.pre_arrival_risk
        if scenario is not None:
            scenario.post_arrival_risk = estimate.post_arrival_risk
    result.risk_status = RiskStatus.OK
    result.risk_basis = selection.basis


def _apply_accuracy(result, request, settings, context, indexed, selector=None):
    """동일 산식·H의 확정 TUNE 커버 실적과 D 비율의 LOW 범위를 연결한다(개·비율)."""
    if result.status not in (ResultStatus.OK, ResultStatus.NOT_NEEDED) or context is None:
        return
    from engine.evaluation import accuracy_for, case_from_sample
    samples = [s for b in context.batches for s in b.samples
               if s.product_id == result.product_id and s.method == result.method
               and s.horizon_days == result.horizon_days and s.target_end <= request.as_of_date]
    cases = [case for s in samples if (case := case_from_sample(s, indexed)) is not None]
    accuracy = accuracy_for(result.product_id, result.valid_observation_days, cases, settings,
                            request.policy.multiplier, result.method, result.horizon_days, context,
                            request.as_of_date)
    result.accuracy_history = accuracy.history
    result.accuracy_grade = accuracy.grade
    result.cover_success_count = accuracy.cover_success_count
    result.cover_trial_count = accuracy.cover_trial_count
    result.forecast_error_rate = accuracy.forecast_error_rate
    if result.accuracy_grade is not None and result.accuracy_grade.value == "LOW":
        choose = selector.select if selector is not None else lambda **kwargs: select_risk_samples(context, **kwargs)
        selection = choose(product_id=result.product_id, method=result.method,
            horizon_days=(request.today-request.as_of_date).days + result.horizon_days,
            observed_through=request.as_of_date)
        if selection.basis is not None:
            result.forecast_range_low, result.forecast_range_high = empirical_range(
                result.forecast_outbound_qty, selection.ratios)


def calculate(
    request: CalculateRequest, product_daily: pd.DataFrame, *, risk_context: RiskContext | None = None,
) -> CalculateResponse:
    """D=g+H일 예측에서 H일 필요량(개)을 구하고, 권장량의 80/100/120%를 입고 단위로 올려 비교한다."""
    settings, daily, ids = _inputs(request, product_daily)
    results = []
    indexed = {}
    selector = _RiskSelector(risk_context) if risk_context is not None else None
    if risk_context is not None:
        from engine.evaluation import index_daily
        indexed = index_daily(daily[(daily.stock_date >= risk_context.period_start)
                                    & (daily.stock_date <= min(request.as_of_date, risk_context.period_end))])
    for product_id in ids:
        config = settings.get(product_id)
        result, planned = _product(request, daily, product_id, config)
        result.no_date_receipt_qty = sum(r.quantity for r in request.no_date_receipts if r.product_id == product_id)
        if result.status is ResultStatus.OK:
            for kind, percent in [(ScenarioType.P80, 80), (ScenarioType.P100, 100), (ScenarioType.P120, 120)]:
                qty = round_to_order_unit(result.recommended_qty * percent / 100, config.order_unit_qty)
                result.scenarios.append(_scenario(
                    result, config, request.today, planned, qty, kind,
                    duplicate=kind is not ScenarioType.P100 and qty == result.recommended_qty,
                ))
        _apply_accuracy(result, request, config, risk_context, indexed, selector)
        _apply_risk(result, request, daily, config, planned, risk_context, selector)
        result.insight_input = to_insight_input(result, "이 상품", as_of_date=request.as_of_date)
        results.append(result)
    return CalculateResponse(engine_version=ENGINE_VERSION, results=results)


def custom_scenario(
    request: ScenarioRequest, product_daily: pd.DataFrame, *, risk_context: RiskContext | None = None,
) -> ScenarioResult:
    """한 상품의 같은 입력 snapshot으로 재계산하고, 직접 입력한 수량(개)을 그대로 시뮬레이션한다."""
    ci = request.calculation_input
    settings, daily, ids = _inputs(ci, product_daily)
    if len(ids) != 1:
        raise CalculationInputError("직접 입력 시나리오는 상품 1개의 계산 입력이 필요해요")
    product_id = ids[0]
    config = settings.get(product_id)
    result, planned = _product(ci, daily, product_id, config)
    if result.status is ResultStatus.NOT_CALCULABLE:
        raise CalculationInputError(result.reason)
    quantities = {
        round_to_order_unit(result.recommended_qty * percent / 100, config.order_unit_qty)
        for percent in (80, 100, 120)
    } if result.recommended_qty else set()
    scenario = _scenario(result, config, ci.today, planned, request.qty, ScenarioType.CUSTOM, request.qty in quantities)
    result.scenarios = [scenario]
    _apply_risk(result, ci, daily, config, planned, risk_context)
    return scenario
