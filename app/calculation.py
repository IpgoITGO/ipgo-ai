"""HTTP와 SQS의 공통 계산 경로. 읽기 권한을 확인한 동일 DataFrame만 표본과 계산에 사용한다."""
import pandas as pd

from app.config import get_settings
from app.execution import HORIZON_TOO_LONG_REASON, oversized_products
from app.risk_policy import load_risk_policy
from app.source_digest import source_digest
from engine import calculate as core
from engine.insight_facts import to_insight_input
from engine.risk_sampling import prepare_risk_context
from engine.scenario import RiskInputError
from schemas.base import Method
from schemas.engine import AccuracyEvaluation, CalculateRequest, CalculateResponse, ScenarioRequest, ScenarioResult


class ModelBundleLoadError(core.CalculationInputError):
    """모델 연결 실패를 구분하되 기존 HTTP 입력 오류 계약은 유지한다."""


def _context(request: CalculateRequest, daily: pd.DataFrame):
    if request.policy.default_method is Method.LGBM19:
        return None
    policy = load_risk_policy()
    return prepare_risk_context(request, daily, policy) if policy is not None else None


def _model_bundle(request):
    if request.policy.default_method is not Method.LGBM19:
        return None
    from app.config import get_settings
    from engine.model19 import Model19Error, load_bundle
    try:
        return load_bundle(get_settings().model19_directory, request.policy.model_version or "")
    except Model19Error as exc:
        raise ModelBundleLoadError(str(exc)) from exc


def _without_oversized(request: CalculateRequest, daily: pd.DataFrame):
    """366일을 넘는 상품을 뺀 계산 요청과, 원래 요청의 상품 순서·제외 상품을 돌려준다(#141).

    빼는 상품은 입고 설정도 함께 빼서 위험 표본 길이(H, D)에 넣지 않는다.
    상품 순서는 엔진과 같은 `_inputs` 규칙으로 정하고, 제외 상품은 productIds로 명시해 막는다.
    """
    oversized = oversized_products(request)
    if not oversized:
        return request, None, set()
    _, _, ids = core._inputs(request, daily)
    narrowed = request.model_copy(update={
        "product_ids": [p for p in ids if p not in oversized],
        "settings": [s for s in request.settings if s.product_id not in oversized],
    })
    return narrowed, ids, oversized


def _horizon_too_long(request: CalculateRequest, product_id: int, model_insight_enabled: bool):
    """엔진의 계산 불가 결과와 같은 모양으로 만든다. 입고 예정 수량·인사이트 입력도 엔진과 같게 채운다."""
    result, _ = core._not_calculable(product_id, HORIZON_TOO_LONG_REASON)
    result.no_date_receipt_qty = sum(r.quantity for r in request.no_date_receipts if r.product_id == product_id)
    result.insight_input = to_insight_input(result, "이 상품", as_of_date=request.as_of_date,
                                            model_insight_enabled=model_insight_enabled)
    return result


def calculate(request: CalculateRequest, daily: pd.DataFrame) -> CalculateResponse:
    """업로드 snapshot의 TUNE 표본을 준비해 DA07과 기존 수량 계산에 연결한다."""
    try:
        insight_enabled = get_settings().model_insight_enabled
        engine_request, ids, oversized = _without_oversized(request, daily)
        context = _context(engine_request, daily)
        response = core.calculate(engine_request, daily, risk_context=context,
                                  model_bundle=_model_bundle(request), model_insight_enabled=insight_enabled)
        if oversized:
            computed = {result.product_id: result for result in response.results}
            response.results = [
                _horizon_too_long(request, product_id, insight_enabled) if product_id in oversized
                else computed[product_id] for product_id in ids]
        if request.policy.default_method is Method.LGBM19:
            response.engine_version += "+lgbm19"
        # 선택 source·기준일 범위와 v1 지문 계약은 유지한다.
        _, source_daily, _ = core._inputs(request, daily)
        digest = source_digest(source_daily)
        for result in response.results:
            capacity_exceeded = context is not None and result.horizon_days in context.unavailable_horizons
            evaluated = (context is not None and not capacity_exceeded
                         and result.status.value in ("OK", "NOT_NEEDED"))
            history = sorted(result.accuracy_history, key=lambda point: point.origin_date)
            result.accuracy_evaluation = AccuracyEvaluation(
                status="AVAILABLE" if history else "EMPTY" if evaluated else "UNAVAILABLE",
                reason=(None if history else "EVALUATION_CAPACITY_EXCEEDED" if capacity_exceeded
                        else "NO_VALID_SAMPLES" if evaluated else "EVALUATION_UNAVAILABLE"),
                period_start=context.period_start if context else None,
                period_end=context.period_end if context else None,
                observed_through=request.as_of_date, horizon_days=result.horizon_days,
                method=result.method, multiplier=request.policy.multiplier,
                policy_version=context.policy_version if context else None,
                policy_hash=context.policy_hash if context else None, data_hash=digest, history=history)
        return response
    except RiskInputError as exc:
        raise core.CalculationInputError("위험 계산 입력을 확인해 주세요") from exc


def custom_scenario(request: ScenarioRequest, daily: pd.DataFrame) -> ScenarioResult:
    """계산과 동일 정책·source·기간으로 직접 입력 시나리오 위험을 재계산한다."""
    try:
        ci = request.calculation_input
        engine_input, ids, oversized = _without_oversized(ci, daily)
        # 상품이 1개가 아니면 엔진이 기존 문장으로 거부하도록 원래 입력을 그대로 넘긴다.
        if oversized and len(ids) == 1:
            # 기존처럼 그 상품이 계산 불가면 직접 시나리오는 이유와 함께 입력 오류로 돌려준다.
            if ids[0] in oversized:
                raise core.CalculationInputError(HORIZON_TOO_LONG_REASON)
            request = request.model_copy(update={"calculation_input": engine_input})
        context = _context(request.calculation_input, daily)
        return core.custom_scenario(
            request, daily, risk_context=context, model_bundle=_model_bundle(request.calculation_input))
    except RiskInputError as exc:
        raise core.CalculationInputError("위험 계산 입력을 확인해 주세요") from exc
