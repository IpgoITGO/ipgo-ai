"""프로세스의 대용량 엔진 작업을 한 번에 하나만 실행한다."""
from contextlib import contextmanager
from threading import Lock

from app.errors import AppError
from engine.calculate import CalculationInputError

_lock = Lock()


@contextmanager
def engine_slot():
    if not _lock.acquire(blocking=False):
        raise AppError(503, "ENGINE_BUSY", "계산 엔진이 사용 중이에요. 잠시 후 다시 시도해 주세요.")
    try:
        yield
    finally:
        _lock.release()


def validate_calculation_capacity(request):
    """현재 서비스의 계산 용량 제한을 미리 확인한다."""
    gap = (request.today - request.as_of_date).days
    if gap > 730:
        raise CalculationInputError("기준일과 오늘 사이 간격은 최대 730일을 지원해요")
    settings = request.settings
    if any(s.lead_time_days + s.review_cycle_days > 366 for s in settings):
        raise CalculationInputError("상품별 계산 기간은 최대 366일을 지원해요")
    selected = set(request.product_ids) if request.product_ids is not None else None
    points = sum(3 * (s.lead_time_days + s.review_cycle_days) for s in settings
                 if selected is None or s.product_id in selected)
    if points > 10_000:
        raise CalculationInputError("시나리오 예상 결과의 지원 크기를 초과했어요")
