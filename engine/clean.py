"""DA-01 수불부 정제 · 재고 검산."""
import re
from datetime import date, datetime, timedelta
from io import BytesIO
from math import floor
from statistics import median
from zipfile import BadZipFile, ZipFile

import openpyxl
import pandas as pd

from engine.calculate import CalculationInputError
from engine.observation import mark_valid_observations
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
# 없어도 되는 열. 유통기한은 재고관리단위마다 하나이고, 비었거나 날짜로 못 읽으면 None이다(#97).
OPTIONAL_HEADERS = {"유통기한": "expiry_date"}
EXPIRY_DATE = re.compile(r"^\s*(\d{4})(?:[-./](\d{1,2})[-./](\d{1,2})|(\d{2})(\d{2}))(?!\d)")
DATA_START_ROW = 5  # 1행 열 이름 · 2행 'DD 일' · 3행 입고 · 출고 · 조정 · 재고 · 4행 빈 줄
YEAR_MONTH = re.compile(r"(?<!\d)(20\d{2})[-_. ]?(0[1-9]|1[0-2])(?!\d)")
KOREAN_MONTH = re.compile(r"(?<!\d)(20\d{2})\s*년\s*(0?[1-9]|1[0-2])\s*월")
DAY_LABEL = re.compile(r"^\s*(\d{1,2})\s*일\s*$")


class CleaningInputError(CalculationInputError):
    """원본 또는 정제 입력이 유효하지 않다.

    메시지는 셀러에게 그대로 보이는 문장이다(worker가 결과에 담는다, #94). reason은 화면이 구분할 수 있는 코드다.
    """

    def __init__(self, message: str, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


MAX_QTY = 999_999_999_999_999
MAX_UNCOMPRESSED = 16 * 1024 * 1024
MAX_SOURCE_ROWS = 100_000
MAX_UNIT_DAYS = 20_000
MAX_PRODUCT_DAYS = 10_000
MAX_COLUMNS = 256


def clean(files: list[tuple[SourceFile, bytes]], known_barcodes: set[str],
          previous_units: dict[tuple[str, date], int] | None = None,
          period: tuple[date, date] | None = None) -> CleanResponse:
    """원본 한 파일을 정제한다. 재고 수량 단위는 개이다."""
    if len(files) != 1:
        raise CleaningInputError("원본 파일은 정확히 하나여야 해요")
    source, content = files[0]
    try:
        try:
            units = read_ledger(content, source.original_name, quarantine_pre_receipt=True)
        except CleaningInputError:
            raise
        except Exception as exc:
            raise CleaningInputError("수불부 형식 또는 값이 올바르지 않아요") from exc
        if units.empty:
            raise CleaningInputError("수불부에 재고 기록이 없어요")
        if period and (units.stock_date.min() < period[0] or units.stock_date.max() > period[1]):
            raise CleaningInputError("수불부 날짜가 업로드 기간을 벗어났어요")
        if previous_units and units.stock_date.min().day != 1:
            raise CleaningInputError("전월 재고와 연결할 때 이번 자료는 1일부터 연속되어야 해요")
        metadata = units[[*UNIT_KEY, "sku_barcode", "sku_name", "unit_cost", "expiry_date", "source_row"]].drop_duplicates()
        if metadata.duplicated(UNIT_KEY).any():
            raise CleaningInputError("재고관리단위가 중복되었어요")
        if metadata.groupby("sku_barcode")["unit_cost"].nunique(dropna=False).gt(1).any():
            raise CleaningInputError("같은 바코드에 서로 다른 원가가 있어요")
        if metadata.groupby("sku_barcode")["sku_name"].nunique().gt(1).any():
            raise CleaningInputError("같은 바코드에 서로 다른 상품명이 있어요")
        total = sum(abs(int(v)) for c in [*MOVES, "closing_qty"] for v in units[c])
        total += sum(previous_units.values()) if previous_units else 0
        if total > MAX_QTY:
            raise CleaningInputError("재고 수량 합계가 안전한 정수 범위를 벗어났어요")
        quarantine = units[units.stock_date < units.received_on]
        quarantined = set(quarantine.sku_barcode)
        grid = _unit_days(units, previous_units)
        if (grid.opening_qty < 0).any():
            raise CleaningInputError("기초 재고는 음수일 수 없어요")
        if previous_units:
            present = set(zip(units.source_inventory_key, units.received_on, strict=True))
            missing = sum(1 for key, qty in previous_units.items() if qty and key not in present)
            if missing:
                raise CleaningInputError(
                    f"전월 잔여 재고의 재고관리단위가 이번 자료에 없어요({missing}개). "
                    "전월 자료와 같은 셀러·창고의 수불부인지 확인해 주세요",
                    reason="PREVIOUS_MONTH_MISMATCH",
                )
        bad = _bad_from_grid(grid)
        product_daily = mark_valid_observations(_product_from_grid(grid))
        unsafe = product_daily.sku_barcode.isin(quarantined | set(bad.sku_barcode))
        product_daily.loc[unsafe, "is_valid_observation"] = False
        product_daily.loc[unsafe, "observation_note"] = "원본 검증 제외 SKU(해당 파일 전체 미관측)"
        product_info = metadata.drop_duplicates("sku_barcode").set_index("sku_barcode")
        products = [dict(sku_barcode=bc, sku_name=row.sku_name,
                         unit_cost=None if pd.isna(row.unit_cost) else int(row.unit_cost),
                         is_new=bc not in known_barcodes)
                    for bc, row in product_info.iterrows()]
        inventory_units = [{**u, "expiry_date": None if pd.isna(u["expiry_date"]) else u["expiry_date"]}
                           for u in metadata[[*UNIT_KEY, "sku_barcode", "expiry_date"]].to_dict("records")]
        records = units[units.stock_date >= units.received_on].merge(grid[[*UNIT_KEY, "stock_date", "opening_qty"]],
                              on=[*UNIT_KEY, "stock_date"], validate="one_to_one")
        records = records.drop(columns=["source_file", "sku_barcode", "sku_name", "unit_cost", "expiry_date"])
        records["upload_file_id"] = source.upload_file_id
        product_daily["upload_file_id"] = source.upload_file_id
        excluded = []
        for row in quarantine.sort_values(["stock_date", "source_row"]).drop_duplicates("sku_barcode").itertuples():
            excluded.append(dict(sku_barcode=row.sku_barcode, sku_name=product_info.loc[row.sku_barcode, "sku_name"],
                                 date=row.stock_date,
                                 reason=(f"원본 {row.source_row}행에 입고일 이전 기록이 있어 이 상품을 제외했어요. "
                                         "원본 날짜를 확인해 주세요")))
        for row in bad[~bad.sku_barcode.isin(quarantined)].drop_duplicates("sku_barcode").itertuples():
            excluded.append(dict(sku_barcode=row.sku_barcode, sku_name=product_info.loc[row.sku_barcode, "sku_name"],
                                 date=row.stock_date, reason=row.reason))
        return CleanResponse(period_start=units.stock_date.min(), period_end=units.stock_date.max(),
                             as_of_date=units.stock_date.max(), products=products, inventory_units=inventory_units,
                             unit_daily_records=records.to_dict("records"),
                             product_daily_stocks=product_daily.to_dict("records"),
                             validation=dict(checked_sku_count=len(products),
                                             target_sku_count=len(products) - len(excluded),
                                             excluded_skus=excluded),
                             estimated_review_cycle_days=estimate_review_cycle_days(
                                 product_daily, quarantined | set(bad.sku_barcode)))
    except CleaningInputError:
        raise
    except (ValueError, TypeError, IndexError, KeyError, OverflowError, OSError, EOFError, BadZipFile) as exc:
        raise CleaningInputError("수불부 형식 또는 값이 올바르지 않아요") from exc


def estimate_review_cycle_days(product_daily: pd.DataFrame, excluded_barcodes: set[str]) -> int | None:
    """파일 안 SKU별 입고일 3개 이상일 때 간격 중앙값의 중앙값을 제안한다.

    수요 관측 flag는 입고 검산과 다르므로 사용하지 않는다. 같은 날 여러 LOT는
    하나의 입고일이며, 제외 SKU와 입고 없는 날은 표본에 넣지 않는다.
    """
    dates_by_sku: dict[str, set[date]] = {}
    for row in product_daily.itertuples():
        if row.sku_barcode not in excluded_barcodes and row.inbound_qty > 0:
            dates_by_sku.setdefault(row.sku_barcode, set()).add(row.stock_date)
    cycles = []
    for dates in dates_by_sku.values():
        ordered = sorted(dates)
        if len(ordered) >= 3:
            cycles.append(median((b - a).days for a, b in zip(ordered, ordered[1:], strict=False)))
    return floor(median(cycles) + 0.5) if cycles else None


def read_ledger(content: bytes, original_name: str, *, quarantine_pre_receipt: bool = False) -> pd.DataFrame:
    """엑셀 수불부 1개(재고관리단위 × 날짜 가로형)를 세로형으로 바꾼다. 연 · 월은 파일명(YYYY-MM · YYYYMM)에서 읽는다.

    반환 칼럼: source_file, source_sheet, source_row, source_inventory_key(sku_mixed_id), received_on,
    sku_barcode, sku_name, unit_cost, stock_date, inbound_qty, outbound_qty, adjustment_qty, closing_qty
    빈칸(입고 전 · 소진 후, 4칸이 함께 빔)인 날은 행을 만들지 않는다. 기초 재고는 여러 파일을 합친 뒤 정한다.
    수량 단위: 개.
    """
    months = {(int(m.group(1)), int(m.group(2))) for pattern in (YEAR_MONTH, KOREAN_MONTH)
              for m in pattern.finditer(original_name)}
    if len(months) != 1:
        raise CleaningInputError("파일명에 유효한 연월 하나가 필요해요")
    year, month = months.pop()

    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if (sum(info.file_size for info in entries) > MAX_UNCOMPRESSED
                    or any(info.file_size > 1_000_000 and info.file_size > 1000 * max(info.compress_size, 1)
                           for info in entries)):
                raise CleaningInputError("XLSX 압축 해제 크기 제한을 초과했어요")
        wb = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise CleaningInputError("올바른 XLSX 파일이 아니에요") from exc
    try:
        ws = wb.worksheets[0]
        if not ws.title.strip() or len(ws.title) > 255:
            raise CleaningInputError("시트 이름이 올바르지 않아요")
        ws.reset_dimensions()  # 작성자가 위조한 dimension을 신뢰하지 않는다.
        rows = ws.iter_rows(max_col=MAX_COLUMNS, values_only=True)
        try:
            header, day_row = next(rows), next(rows)
            next(rows)
            next(rows)
        except StopIteration as exc:
            raise CleaningInputError("수불부 헤더가 부족해요") from exc
        id_cols = {ID_HEADERS[v]: i for i, v in enumerate(header) if v in ID_HEADERS}
        missing = set(ID_HEADERS.values()) - set(id_cols)
        if missing:
            raise ValueError(f"수불부 열을 찾을 수 없어요: {sorted(missing)}")
        optional_cols = {OPTIONAL_HEADERS[v]: i for i, v in enumerate(header) if v in OPTIONAL_HEADERS}
        days = [  # (날짜, 입고 칸 위치). 한 날짜는 입고 · 출고 · 조정 · 재고 4칸
            (date(year, month, int(m.group(1))), i)
            for i, v in enumerate(day_row)
            if isinstance(v, str) and (m := DAY_LABEL.match(v))
        ]

        if not days or len({d for d, _ in days}) != len(days):
            raise CleaningInputError("날짜 열이 없거나 중복되었어요")
        records = []
        seen_units = set()
        seen_products = set()
        for row_no, row in enumerate(rows, DATA_START_ROW):
            if row_no > MAX_SOURCE_ROWS + DATA_START_ROW - 1:
                raise CleaningInputError("원본 행 수 제한을 초과했어요")
            if row[id_cols["sku_barcode"]] is None:
                if any(row[id_cols[name]] is not None for name in ("source_inventory_key", "sku_name", "received_on")):
                    raise CleaningInputError("바코드가 없는 재고 기록이 있어요")
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
                "expiry_date": (_to_expiry(row[optional_cols["expiry_date"]])
                                if "expiry_date" in optional_cols and optional_cols["expiry_date"] < len(row) else None),
            }
            if any(
                not str(unit[k]).strip() or str(unit[k]) == "None"
                or len(str(unit[k]).encode("utf-16-le")) // 2 > limit
                for k, limit in (("source_inventory_key", 100), ("sku_barcode", 100), ("sku_name", 200))
            ):
                raise CleaningInputError("상품 또는 재고관리단위 정보가 없어요")
            if unit["source_inventory_key"] != unit["source_inventory_key"].strip():
                raise CleaningInputError("재고관리단위 식별자가 올바르지 않아요")
            key = (unit["source_inventory_key"], unit["received_on"])
            if key in seen_units:
                raise CleaningInputError("재고관리단위가 중복되었어요")
            seen_units.add(key)
            seen_products.add(unit["sku_barcode"])
            span = (max(d for d, _ in days) - min(d for d, _ in days)).days + 1
            if len(seen_units) * span > MAX_UNIT_DAYS or len(seen_products) * span > MAX_PRODUCT_DAYS:
                raise CleaningInputError("정제 집계 크기 제한을 초과했어요")
            if unit["unit_cost"] is not None:
                unit["unit_cost"] = _quantity(unit["unit_cost"], False)
            for stock_date, col in days:
                cells = row[col:col + 4]
                if all(c is None for c in cells):
                    continue
                if stock_date < unit["received_on"] and not quarantine_pre_receipt:
                    raise CleaningInputError("재고 날짜가 입고일보다 빨라요")
                if len(cells) != 4:
                    raise CleaningInputError("날짜별 수량 열이 부족해요")
                inbound, outbound, adjustment, closing = (_quantity(c if c is not None else 0, i == 2)
                                                           for i, c in enumerate(cells))
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
            "sku_barcode", "sku_name", "unit_cost", "expiry_date", "stock_date", *MOVES, "closing_qty",
        ])

    finally:
        wb.close()

def check_stock_balance(units: pd.DataFrame, previous_units=None) -> pd.DataFrame:
    """재고관리단위마다 '기초 + 입고 − 출고 + 조정 = 기말'을 검산한다. units: read_ledger 결과를 합친 표.

    기초 = 전날 기말(빈칸 · 파일에 없음은 0). 자료 첫날만 '기말 − 입고 + 출고 − 조정'으로 거꾸로 구한다.
    기말이 0이 아닌데 다음 날 기록이 없거나, 자료 첫날이 아닌데 처음 나타난 단위의 기초가 0이 아니어도 불일치.
    반환: 불일치 행 (sku_barcode, source_inventory_key, received_on, stock_date,
    diff_qty = 기록 기말 − 계산 기말, reason). 수량 단위: 개.
    """
    grid = _unit_days(units, previous_units)
    return _bad_from_grid(grid)


def _bad_from_grid(grid: pd.DataFrame) -> pd.DataFrame:
    bad = grid[grid["diff_qty"] != 0].copy()
    bad["reason"] = [_mismatch_reason(r) for r in bad.itertuples()]
    return bad[["sku_barcode", *UNIT_KEY, "stock_date", "diff_qty", "reason"]].reset_index(drop=True)


def to_product_daily(units: pd.DataFrame, previous_units=None) -> pd.DataFrame:
    """재고관리단위를 상품(SKU바코드) 단위로 날짜별 합산한다. 자료 기간의 모든 날짜에 행을 만들고 기록이 없으면 0.

    반환 칼럼: sku_barcode, stock_date, opening_qty, inbound_qty, outbound_qty, adjustment_qty, closing_qty
    상품 값 = 그 상품 재고관리단위 값의 합 (기초 재고도 단위별 기초의 합 = 상품의 전날 기록 기말). 수량 단위: 개.
    """
    grid = _unit_days(units, previous_units)
    return _product_from_grid(grid)


def _product_from_grid(grid: pd.DataFrame) -> pd.DataFrame:
    qty = ["opening_qty", *MOVES, "closing_qty"]
    return grid.groupby(["sku_barcode", "stock_date"], as_index=False)[qty].sum()


def _unit_days(units: pd.DataFrame, previous_units=None) -> pd.DataFrame:
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
    if previous_units:
        first = grid["stock_date"] == start
        for index in grid.index[first]:
            row = grid.loc[index]
            key = (row.source_inventory_key, row.received_on)
            if key in previous_units:
                grid.at[index, "opening_qty"] = previous_units[key]
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


def _to_expiry(value) -> date | None:
    """유통기한 칸 → 날짜. 예: '2027-03-15 00:00:00.000 Asia/Seoul' → 2027-03-15.

    유통기한은 선택 정보라 비었거나('-' 포함) 날짜로 못 읽으면 정제를 막지 않고 None으로 둔다(#97).
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    match = EXPIRY_DATE.match(str(value)[:40])
    if not match:
        return None
    year, month, day = (int(match[1]), int(match[2] or match[4]), int(match[3] or match[5]))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _to_text(value) -> str:
    """바코드가 숫자 칸으로 읽혀도 '8800…' 문자열로 맞춘다."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _quantity(value, signed: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CleaningInputError("재고 수량은 정수여야 해요")
    if not (-MAX_QTY if signed else 0) <= value <= MAX_QTY or int(value) != value:
        raise CleaningInputError("재고 수량이 허용 범위를 벗어났어요")
    return int(value)
