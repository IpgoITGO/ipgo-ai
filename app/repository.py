"""Spring이 제공한 안정된 view를 read-only transaction으로 조회한다."""
import pandas as pd
import psycopg

from app.config import get_settings
from app.errors import AppError


def load_cleaning_context(user_id: int, warehouse_id: int, upload_id: int,
                          previous_upload_file_id: int | None, source):
    """정제 원본과 직전 월 최신 revision을 읽고 요청 소유권을 검증한다."""
    from datetime import date, timedelta

    from engine.clean import MAX_QTY, CleaningInputError

    settings = get_settings()
    if settings.database_url is None:
        if source.upload_file_id != upload_id:
            raise CleaningInputError("업로드 식별자가 일치하지 않아요")
        return set(), {}, None
    try:
        with psycopg.connect(settings.database_url, autocommit=False, connect_timeout=5) as connection:
            connection.read_only = True
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '10s'")
                    cursor.execute("""SELECT upload_file_id, user_id, warehouse_id, storage_key, original_name,
                                             period_start, period_end, revision_no, status
                                      FROM ai_cleaning_sources_v1 WHERE upload_file_id = %s""", (upload_id,))
                    current = cursor.fetchone()
                    if (not current or current[0] != source.upload_file_id or current[1] != user_id
                            or current[2] != warehouse_id or current[3] != source.storage_key
                            or current[4] != source.original_name or current[8] != "PROCESSING"):
                        raise CleaningInputError("정제 원본 또는 소유권이 올바르지 않아요")
                    start, end = current[5:7]
                    if not isinstance(start, date) or not isinstance(end, date) or start > end:
                        raise CleaningInputError("원본 기간이 올바르지 않아요")
                    prior_end = start.replace(day=1) - timedelta(days=1)
                    prior_start = prior_end.replace(day=1)
                    cursor.execute("""SELECT upload_file_id, user_id, warehouse_id, storage_key, original_name,
                                             period_start, period_end, revision_no, status
                                      FROM ai_cleaning_sources_v1
                                      WHERE user_id = %s AND warehouse_id = %s
                                        AND period_start >= %s AND period_start <= %s
                                      ORDER BY revision_no DESC, upload_file_id DESC LIMIT 1""",
                                   (user_id, warehouse_id, prior_start, prior_end))
                    prior = cursor.fetchone()
                    expected_id = prior[0] if prior and prior[8] == "DONE" else None
                    if previous_upload_file_id != expected_id:
                        raise CleaningInputError("직전 월 최신 업로드가 요청과 일치하지 않아요")
                    cursor.execute("SELECT sku_barcode FROM ai_cleaning_products_v1 WHERE user_id = %s", (user_id,))
                    barcodes = {str(row[0]) for row in cursor.fetchall()}
                    previous = {}
                    if expected_id is not None:
                        if prior[1] != user_id or prior[2] != warehouse_id or prior[6] != prior_end:
                            raise CleaningInputError("직전 월 원본 기간 또는 소유권이 올바르지 않아요")
                        cursor.execute("""SELECT source_inventory_key, received_on, stock_date, closing_qty
                                          FROM ai_cleaning_previous_units_v1
                                          WHERE upload_file_id = %s AND user_id = %s AND warehouse_id = %s
                                          ORDER BY source_inventory_key, received_on, stock_date DESC""",
                                       (expected_id, user_id, warehouse_id))
                        dates_seen = {}
                        for key, received, stock_date, qty in cursor.fetchall():
                            if (key, received) in previous:
                                if dates_seen[key, received] == stock_date:
                                    raise CleaningInputError("직전 월 재고 날짜가 중복되었어요")
                                continue
                            if stock_date != prior_end or not key or not isinstance(received, date):
                                raise CleaningInputError("직전 월 재고 날짜가 월말과 일치하지 않아요")
                            if isinstance(qty, bool) or not isinstance(qty, int) or not 0 <= qty <= MAX_QTY:
                                raise CleaningInputError("직전 월 재고 수량이 올바르지 않아요")
                            previous[key, received] = qty
                            dates_seen[key, received] = stock_date
                        if not previous:
                            raise CleaningInputError("직전 월 재고 기록이 없어 연결할 수 없어요")
            finally:
                connection.rollback()
        return barcodes, previous, (start, end)
    except psycopg.Error as exception:
        raise AppError(503, "DATABASE_UNAVAILABLE", "정제 데이터를 읽지 못했어요") from exception

def load_product_daily(warehouse_id: int, upload_file_ids: list[int], *, user_id: int) -> pd.DataFrame:
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
        with psycopg.connect(settings.database_url, autocommit=False, connect_timeout=5) as connection:
            connection.read_only = True
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '10s'")
                    cursor.execute("""SELECT upload_file_id, user_id, warehouse_id, status
                                      FROM ai_cleaning_sources_v1 WHERE upload_file_id = ANY(%s)""",
                                   (upload_file_ids,))
                    sources = cursor.fetchall()
                    requested = set(upload_file_ids)
                    if (len(sources) != len(requested) or len(requested) != len(upload_file_ids)
                            or {row[0] for row in sources} != requested
                            or any(row[1:] != (user_id, warehouse_id, "DONE") for row in sources)):
                        from engine.calculate import CalculationInputError
                        raise CalculationInputError("원본의 소유권, 창고 또는 완료 상태가 올바르지 않아요")
                    cursor.execute(query, (warehouse_id, upload_file_ids))
                    rows = cursor.fetchall()
                    columns = [column.name for column in cursor.description or []]
            finally:
                connection.rollback()
        return pd.DataFrame(rows, columns=columns)
    except psycopg.Error as exception:
        raise AppError(
            503,
            "DATABASE_UNAVAILABLE",
            "계산 데이터를 읽지 못했어요",
            {"warehouseId": warehouse_id, "uploadFileIds": upload_file_ids},
        ) from exception
