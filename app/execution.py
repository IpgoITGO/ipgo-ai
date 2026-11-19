"""프로세스의 대용량 엔진 작업을 한 번에 하나만 실행한다."""
from contextlib import contextmanager
from threading import Lock

from app.errors import AppError
from engine.calculate import CalculationInputError

_lock = Lock()
MAX_SCENARIO_POINTS = 60_000
MAX_PRODUCT_HORIZON_DAYS = 366
HORIZON_TOO_LONG_REASON = "리드타임과 입고 주기를 합쳐 366일 이하로 넣어 주세요"


@contextmanager
def engine_slot():
    if not _lock.acquire(blocking=False):
        raise AppError(503, "ENGINE_BUSY", "계산 엔진이 사용 중이에요. 잠시 후 다시 시도해 주세요.")
    try:
        yield
    finally:
        _lock.release()


def oversized_products(request) -> set[int]:
    """리드타임 + 입고 주기(일)가 366일을 넘는 상품 ID. 이 상품만 계산 불가로 돌려준다(#141)."""
    return {s.product_id for s in request.settings
            if s.lead_time_days + s.review_cycle_days > MAX_PRODUCT_HORIZON_DAYS}


def validate_calculation_capacity(request):
    """요청 전체의 계산 용량 제한을 미리 확인한다. 넘으면 요청 전체가 실패한다.

    시나리오 점수(개) = Σ 3 × (리드타임 + 입고 주기)일, 선택한 상품만 합산한다.
    366일을 넘는 상품은 시나리오를 만들지 않으므로 합산하지 않는다.
    """
    gap = (request.today - request.as_of_date).days
    if gap > 730:
        raise CalculationInputError("기준일과 오늘 사이 간격은 최대 730일을 지원해요")
    oversized = oversized_products(request)
    selected = set(request.product_ids) if request.product_ids is not None else None
    points = sum(3 * (s.lead_time_days + s.review_cycle_days) for s in request.settings
                 if (selected is None or s.product_id in selected) and s.product_id not in oversized)
    if points > MAX_SCENARIO_POINTS:
        raise CalculationInputError("시나리오 예상 결과의 지원 크기를 초과했어요")
