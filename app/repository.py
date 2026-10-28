"""DB 읽기 (읽기 전용). 쓰기는 Spring만 한다. 접속 방식이 정해지기 전까지는 예외로 처리한다."""
import pandas as pd

from app.config import get_settings
from app.errors import AppError


def load_known_barcodes(user_id: int) -> set[str]:
    """이 회원의 이미 등록된 상품 바코드 (CleanedProduct.is_new 판정용). DB 연결 전에는 빈 집합 → 모두 새 상품."""
    if get_settings().database_url is None:
        return set()
    # TODO(합의): products 읽기는 DB 접속 방식 확정 후 구현 (docs/decisions/0001)
    raise NotImplementedError("products reader")


def load_product_daily(warehouse_id: int, upload_file_ids: list[int]) -> pd.DataFrame:
    """product_daily_stocks에서 주어진 업로드 파일의 기록만 읽는다."""
    settings = get_settings()
    if settings.database_url is None:
        raise AppError(
            501,
            "DATABASE_NOT_CONFIGURED",
            "계산 엔진의 DB 연결이 아직 설정되지 않았어요",
            {"warehouseId": warehouse_id, "uploadFileIds": upload_file_ids},
        )
    # TODO(합의): PostgreSQL 드라이버와 읽기 전용 계정 확정 후 구현 (docs/decisions/0001)
    raise NotImplementedError("product_daily_stocks reader")
