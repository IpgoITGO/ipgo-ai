"""DA-01 수불부 정제 · 재고 검산."""
import pandas as pd

from schemas.engine import CleanResponse, SourceFile


def clean(files: list[tuple[SourceFile, bytes]], known_barcodes: set[str]) -> CleanResponse:
    """정제 실행. files: (원본 파일 정보, 파일 내용) 목록. 파일 읽기는 app/storage.py가 한다.

    known_barcodes: 이미 등록된 상품 바코드. app 계층이 DB에서 읽어 넘긴다 → CleanedProduct.is_new.
    """
    raise NotImplementedError("DA-01 clean")


def read_ledger(content: bytes, original_name: str) -> pd.DataFrame:
    """엑셀 수불부 1개(재고관리단위 × 날짜 가로형)를 세로형으로 바꾼다. 연 · 월은 파일명(YYYY-MM · YYYYMM)에서 읽는다.

    반환 칼럼: source_file, source_sheet, source_row, source_inventory_key(sku_mixed_id), received_on,
    sku_barcode, sku_name, unit_cost, stock_date, inbound_qty, outbound_qty, adjustment_qty, closing_qty
    빈칸(입고 전 · 소진 후, 4칸이 함께 빔)인 날은 행을 만들지 않는다. 기초 재고는 여러 파일을 합친 뒤 정한다.
    """
    raise NotImplementedError("DA-01 read_ledger")


def check_stock_balance(units: pd.DataFrame) -> pd.DataFrame:
    """재고관리단위마다 '기초 + 입고 − 출고 + 조정 = 기말'을 검산한다. units: read_ledger 결과를 합친 표.

    기초 = 전날 기말(빈칸 · 파일에 없음은 0). 자료 첫날만 '기말 − 입고 + 출고 − 조정'으로 거꾸로 구한다.
    기말이 0이 아닌데 다음 날 기록이 없거나, 자료 첫날이 아닌데 처음 나타난 단위의 기초가 0이 아니어도 불일치.
    반환: 불일치 행 (sku_barcode, source_inventory_key, stock_date, diff_qty = 기록 기말 − 계산 기말, reason).
    """
    raise NotImplementedError("DA-01 check_stock_balance")


def to_product_daily(units: pd.DataFrame) -> pd.DataFrame:
    """재고관리단위를 상품(SKU바코드) 단위로 날짜별 합산한다. 자료 기간의 모든 날짜에 행을 만들고 기록이 없으면 0.

    반환 칼럼: sku_barcode, stock_date, opening_qty, inbound_qty, outbound_qty, adjustment_qty, closing_qty
    완료 조건: 상품 합계 = 재고관리단위 재고의 합.
    """
    raise NotImplementedError("DA-01 to_product_daily")
