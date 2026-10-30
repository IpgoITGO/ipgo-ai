"""Spring이 제공한 안정된 view를 read-only transaction으로 조회한다."""
import pandas as pd
import psycopg

from app.config import get_settings
from app.errors import AppError


def load_known_barcodes(user_id: int) -> set[str]:
    """이 회원의 이미 등록된 상품 바코드 (CleanedProduct.is_new 판정용). DB 연결 전에는 빈 집합 → 모두 새 상품."""
    if get_settings().database_url is None:
        return set()
    # TODO(합의): products 읽기는 DB 접속 방식 확정 후 구현 (docs/decisions/0001)
    raise NotImplementedError("products reader")


def load_product_daily(warehouse_id: int, upload_file_ids: list[int]) -> pd.DataFrame:
    """ai_product_daily_stocks_v1에서 지정된 source만 읽는다."""
    settings = get_settings()
    if settings.database_url is None:
        raise AppError(
            501,
            "DATABASE_NOT_CONFIGURED",
            "계산 엔진의 DB 연결이 아직 설정되지 않았어요",
            {"warehouseId": warehouse_id, "uploadFileIds": upload_file_ids},
        )
    query = """
        SELECT
            product_id,
            upload_file_id,
            stock_date,
            opening_qty,
            inbound_qty,
            outbound_qty,
            adjustment_qty,
            closing_qty,
            is_valid_observation
        FROM ai_product_daily_stocks_v1
        WHERE warehouse_id = %s
          AND upload_file_id = ANY(%s)
        ORDER BY product_id, stock_date, upload_file_id
    """
    try:
        with psycopg.connect(settings.database_url, autocommit=False) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute(query, (warehouse_id, upload_file_ids))
                rows = cursor.fetchall()
                columns = [column.name for column in cursor.description or []]
            connection.rollback()
        return pd.DataFrame(rows, columns=columns)
    except psycopg.Error as exception:
        raise AppError(
            503,
            "DATABASE_UNAVAILABLE",
            "계산 데이터를 읽지 못했어요",
            {"warehouseId": warehouse_id, "uploadFileIds": upload_file_ids},
        ) from exception
