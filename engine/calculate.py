"""DA-11: 상품별 예측 → 입고 수량 → 시나리오 응답 조립. 위험·등급은 후속 구현."""
from datetime import date, timedelta

import pandas as pd

from engine import ENGINE_VERSION
from engine.forecast import forecast_a, forecast_b
from engine.insight_facts import to_insight_input
from engine.replenish import required_qty, round_to_order_unit, split_forecast, split_planned_receipts, start_stock
from engine.scenario import simulate
from schemas.base import Method, ResultStatus, ScenarioType
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
    if settings is None:
        return _not_calculable(product_id, "상품의 입고 설정이 없어요")
    daily = daily[daily["product_id"] == product_id]
    if daily.empty:
        return _not_calculable(product_id, "선택한 업로드에 기준일까지의 상품 기록이 없어요", 0)
    if daily["stock_date"].duplicated().any():
        return _not_calculable(product_id, "같은 날짜의 상품 기록이 중복됐어요. 유효한 업로드를 확인해 주세요")
    quantities = daily[["outbound_qty", "closing_qty"]].apply(pd.to_numeric, errors="coerce")
    if quantities.isna().any().any() or not ((quantities >= 0) & (quantities < float("inf"))).all().all():
        return _not_calculable(product_id, "출고량과 기말 재고는 0 이상의 유한한 숫자여야 해요")
    if not daily["is_valid_observation"].map(lambda value: isinstance(value, bool)).all():
        return _not_calculable(product_id, "유효 관측일 판정이 없는 기록이 있어요")
    daily = daily.assign(**{column: quantities[column] for column in quantities})
    valid_days = int(daily["is_valid_observation"].sum())
    closing = daily.loc[daily["stock_date"] == request.as_of_date, "closing_qty"]
    if closing.empty:
        return _not_calculable(product_id, "기준일의 기말 재고 기록이 없어요", valid_days)

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
            return _not_calculable(
                product_id, f"{method.value}: {first_reason}; {alternate.value}: {forecast.reason}", valid_days,
            )
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
    shortage = request.today + timedelta(days=baseline.depletion_day) if qty and baseline.depletion_day else None
    # DA-07/13 위험·등급은 검증 후 채우며 0으로 대체하지 않는다.
    return ProductResult(
        product_id=product_id, status=ResultStatus.OK if qty else ResultStatus.NOT_NEEDED,
        method=method, method_reason=method_reason, method_fallback=method != request.policy.default_method,
        multiplier=request.policy.multiplier,
        horizon_days=horizon, forecast_outbound_qty=cover, start_stock_qty=stock, start_stock_estimated=gap > 0,
        planned_receipts_in_horizon=planned_qty, pre_arrival_unmet_qty=baseline.pre_arrival_unmet,
        required_qty_before_rounding=need, recommended_qty=qty, recommended_amount=qty * settings.unit_cost,
        valid_observation_days=valid_days, expected_shortage_on=shortage,
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


def calculate(request: CalculateRequest, product_daily: pd.DataFrame) -> CalculateResponse:
    """D=g+H일 예측에서 H일 필요량(개)을 구하고, 권장량의 80/100/120%를 입고 단위로 올려 비교한다."""
    settings, daily, ids = _inputs(request, product_daily)
    results = []
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
        result.insight_input = to_insight_input(result, "이 상품", as_of_date=request.as_of_date)
        results.append(result)
    return CalculateResponse(engine_version=ENGINE_VERSION, results=results)


def custom_scenario(request: ScenarioRequest, product_daily: pd.DataFrame) -> ScenarioResult:
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
    return _scenario(result, config, ci.today, planned, request.qty, ScenarioType.CUSTOM, request.qty in quantities)
