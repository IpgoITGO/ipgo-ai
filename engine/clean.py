"""DA-01 수불부 정제 · 재고 검산."""
import re
from datetime import date, datetime, timedelta
from io import BytesIO

import openpyxl
import pandas as pd

from schemas.engine import CleanResponse, SourceFile

UNIT_KEY = ["source_inventory_key", "received_on"]  # 재고관리단위 = sku_mixed_id + 입고일자
MOVES = ["inbound_qty", "outbound_qty", "adjustment_qty"]
ID_HEADERS = {
    "SKU바코드": "sku_barcode",
    "입고일자": "received_on",
    "원가": "unit_cost",
    "sku_mixed_id": "source_inventory_key",
    "SKU명": "sku_name",
}
DATA_START_ROW = 5  # 1행 열 이름 · 2행 'DD 일' · 3행 입고 · 출고 · 조정 · 재고 · 4행 빈 줄
YEAR_MONTH = re.compile(r"(?<!\d)(20\d{2})-?(0[1-9]|1[0-2])(?!\d)")
DAY_LABEL = re.compile(r"^\s*(\d{1,2})\s*일\s*$")


def clean(files: list[tuple[SourceFile, bytes]], known_barcodes: set[str]) -> CleanResponse:
    """정제 실행. files: (원본 파일 정보, 파일 내용) 목록. 파일 읽기는 app/storage.py가 한다.

    known_barcodes: 이미 등록된 상품 바코드. app 계층이 DB에서 읽어 넘긴다 → CleanedProduct.is_new.
    """
    # TODO(#15): 정제 요청 단위(파일 1개) · 직전 업로드 기말 재고가 정해진 뒤 응답 조립
    raise NotImplementedError("DA-01 clean")


def read_ledger(content: bytes, original_name: str) -> pd.DataFrame:
    """엑셀 수불부 1개(재고관리단위 × 날짜 가로형)를 세로형으로 바꾼다. 연 · 월은 파일명(YYYY-MM · YYYYMM)에서 읽는다.

    반환 칼럼: source_file, source_sheet, source_row, source_inventory_key(sku_mixed_id), received_on,
    sku_barcode, sku_name, unit_cost, stock_date, inbound_qty, outbound_qty, adjustment_qty, closing_qty
    빈칸(입고 전 · 소진 후, 4칸이 함께 빔)인 날은 행을 만들지 않는다. 기초 재고는 여러 파일을 합친 뒤 정한다.
    수량 단위: 개.
    """
    match = YEAR_MONTH.search(original_name)
    if match is None:
        raise ValueError(f"파일명에서 연 · 월을 읽을 수 없어요: {original_name}")
    year, month = int(match.group(1)), int(match.group(2))

    wb = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    header, day_row = rows[0], rows[1]
    id_cols = {ID_HEADERS[v]: i for i, v in enumerate(header) if v in ID_HEADERS}
    missing = set(ID_HEADERS.values()) - set(id_cols)
    if missing:
        raise ValueError(f"수불부 열을 찾을 수 없어요: {sorted(missing)}")
    days = [  # (날짜, 입고 칸 위치). 한 날짜는 입고 · 출고 · 조정 · 재고 4칸
        (date(year, month, int(m.group(1))), i)
        for i, v in enumerate(day_row)
        if isinstance(v, str) and (m := DAY_LABEL.match(v))
    ]

    records = []
    for row_no, row in enumerate(rows[DATA_START_ROW - 1:], DATA_START_ROW):
        if row[id_cols["sku_barcode"]] is None:
            continue
        unit = {
            "source_file": original_name,
            "source_sheet": ws.title,
            "source_row": row_no,
            "source_inventory_key": str(row[id_cols["source_inventory_key"]]),
            "received_on": _to_date(row[id_cols["received_on"]]),
            "sku_barcode": _to_text(row[id_cols["sku_barcode"]]),
            "sku_name": str(row[id_cols["sku_name"]]),
            "unit_cost": row[id_cols["unit_cost"]],
        }
        for stock_date, col in days:
            cells = row[col:col + 4]
            if all(c is None for c in cells):
                continue
            inbound, outbound, adjustment, closing = (c or 0 for c in cells)
            records.append({
                **unit,
                "stock_date": stock_date,
                "inbound_qty": inbound,
                "outbound_qty": outbound,
                "adjustment_qty": adjustment,
                "closing_qty": closing,
            })
    return pd.DataFrame(records, columns=[
        "source_file", "source_sheet", "source_row", "source_inventory_key", "received_on",
        "sku_barcode", "sku_name", "unit_cost", "stock_date", *MOVES, "closing_qty",
    ])


def check_stock_balance(units: pd.DataFrame) -> pd.DataFrame:
    """재고관리단위마다 '기초 + 입고 − 출고 + 조정 = 기말'을 검산한다. units: read_ledger 결과를 합친 표.

    기초 = 전날 기말(빈칸 · 파일에 없음은 0). 자료 첫날만 '기말 − 입고 + 출고 − 조정'으로 거꾸로 구한다.
    기말이 0이 아닌데 다음 날 기록이 없거나, 자료 첫날이 아닌데 처음 나타난 단위의 기초가 0이 아니어도 불일치.
    반환: 불일치 행 (sku_barcode, source_inventory_key, received_on, stock_date,
    diff_qty = 기록 기말 − 계산 기말, reason). 수량 단위: 개.
    """
    grid = _unit_days(units)
    bad = grid[grid["diff_qty"] != 0].copy()
    bad["reason"] = [_mismatch_reason(r) for r in bad.itertuples()]
    return bad[["sku_barcode", *UNIT_KEY, "stock_date", "diff_qty", "reason"]].reset_index(drop=True)


def to_product_daily(units: pd.DataFrame) -> pd.DataFrame:
    """재고관리단위를 상품(SKU바코드) 단위로 날짜별 합산한다. 자료 기간의 모든 날짜에 행을 만들고 기록이 없으면 0.

    반환 칼럼: sku_barcode, stock_date, opening_qty, inbound_qty, outbound_qty, adjustment_qty, closing_qty
    상품 값 = 그 상품 재고관리단위 값의 합 (기초 재고도 단위별 기초의 합 = 상품의 전날 기록 기말). 수량 단위: 개.
    """
    grid = _unit_days(units)
    qty = ["opening_qty", *MOVES, "closing_qty"]
    return grid.groupby(["sku_barcode", "stock_date"], as_index=False)[qty].sum()


def _unit_days(units: pd.DataFrame) -> pd.DataFrame:
    """재고관리단위 × 자료 기간 모든 날짜 표. 기록이 없는 날은 수량 0 · recorded False.

    opening_qty = 전날 기록 기말 (첫날은 기말 − 입고 + 출고 − 조정)
    calc_closing_qty = opening_qty + 입고 − 출고 + 조정, diff_qty = closing_qty − calc_closing_qty
    """
    start, end = units["stock_date"].min(), units["stock_date"].max()
    dates = pd.DataFrame({"stock_date": [start + timedelta(days=i) for i in range((end - start).days + 1)]})
    keys = units[["sku_barcode", *UNIT_KEY]].drop_duplicates()
    grid = keys.merge(dates, how="cross").merge(
        units[[*UNIT_KEY, "stock_date", *MOVES, "closing_qty"]].assign(recorded=True),
        how="left", on=[*UNIT_KEY, "stock_date"],
    )
    grid["recorded"] = grid["recorded"].fillna(False).astype(bool)
    qty = [*MOVES, "closing_qty"]
    grid[qty] = grid[qty].fillna(0).astype("int64")
    grid = grid.sort_values([*UNIT_KEY, "stock_date"]).reset_index(drop=True)

    net = grid["inbound_qty"] - grid["outbound_qty"] + grid["adjustment_qty"]
    prev_closing = grid.groupby(UNIT_KEY)["closing_qty"].shift(1)
    grid["opening_qty"] = prev_closing.fillna(grid["closing_qty"] - net).astype("int64")
    grid["calc_closing_qty"] = grid["opening_qty"] + net
    grid["diff_qty"] = grid["closing_qty"] - grid["calc_closing_qty"]
    seen = grid.groupby(UNIT_KEY)["recorded"].cumsum()
    grid["first_seen"] = grid["recorded"] & (seen == 1) & (grid["stock_date"] != start)
    return grid


def _mismatch_reason(r) -> str:
    if not r.recorded:
        return f"전날 기말 재고 {r.opening_qty}개가 남았는데 이날 기록이 없어요"
    if r.first_seen:
        return f"자료 첫날이 아닌데 처음 나타났고 기초 재고가 {r.diff_qty}개예요 (전날 기록 없음)"
    return (
        f"기초 {r.opening_qty} + 입고 {r.inbound_qty} − 출고 {r.outbound_qty} + 조정 {r.adjustment_qty}"
        f" = {r.calc_closing_qty}개인데 기록된 기말은 {r.closing_qty}개예요"
    )


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _to_text(value) -> str:
    """바코드가 숫자 칸으로 읽혀도 '8800…' 문자열로 맞춘다."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()
