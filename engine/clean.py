"""DA-01 수불부 정제 · 재고 검산."""
import pandas as pd

from schemas.engine import CleanResponse, SourceFile


def clean(files: list[tuple[SourceFile, bytes]]) -> CleanResponse:
    """정제 실행. files: (원본 파일 정보, 파일 내용) 목록. 파일 읽기는 app/storage.py가 한다."""
    raise NotImplementedError("DA-01 clean")


def read_ledger(content: bytes, original_name: str) -> pd.DataFrame:
    """엑셀 수불부 1개를 재고관리단위 × 날짜 형태의 표로 바꾼다.

    반환 칼럼(제안): source_inventory_key, sku_barcode, sku_name, received_on, stock_date,
    opening_qty, inbound_qty, outbound_qty, adjustment_qty, closing_qty, source_sheet, source_row
    빈칸: 입고 전 · 소진 후 칸은 0으로 채우고, 어느 쪽 빈칸이었는지 표시한다.
    """
    raise NotImplementedError("DA-01 read_ledger")


def check_stock_balance(units: pd.DataFrame) -> pd.DataFrame:
    """재고관리단위 행마다 '전일 재고 + 입고 − 출고 + 조정 = 당일 재고'를 검산한다.

    반환: 불일치 행 (sku_barcode, stock_date, 차이, 사유). 불일치 SKU는 계산에서 제외한다.
    """
    raise NotImplementedError("DA-01 check_stock_balance")


def to_product_daily(units: pd.DataFrame) -> pd.DataFrame:
    """재고관리단위(유통기한별 재고)를 상품(SKU바코드) 단위로 합친다.

    완료 조건: 상품 합계 = 재고관리단위 재고의 합.
    """
    raise NotImplementedError("DA-01 to_product_daily")
