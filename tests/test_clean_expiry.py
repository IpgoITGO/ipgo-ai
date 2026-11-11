"""정제 결과의 재고관리단위별 유통기한(#97). 선택 정보라 비었거나 이상해도 정제를 막지 않는다."""
from datetime import date, datetime
from io import BytesIO

import openpyxl
import pytest

from engine.clean import _to_expiry, clean, read_ledger
from schemas.engine import SourceFile
from tests.ledger_xlsx import build_ledger

RECORDS = {1: (0, 1, 0, 9), 2: (0, 1, 0, 8)}


def _unit(mixed_id, expiry):
    return dict(barcode=f"bc-{mixed_id}", name=f"상품 {mixed_id}", cost=100, mixed_id=mixed_id,
                received_on="2026-05-01", records=RECORDS, expiry=expiry)


def _clean(units):
    raw = build_ledger([1, 2], units)
    source = SourceFile(upload_file_id=1, storage_key="fake", original_name="2026-05.xlsx")
    return clean([(source, raw)], set())


@pytest.mark.parametrize("value, expected", [
    ("2027-03-15 00:00:00.000 Asia/Seoul", date(2027, 3, 15)),  # 실제 수불부 모양
    ("2027.3.5", date(2027, 3, 5)),
    ("20270315", date(2027, 3, 15)),
    (datetime(2027, 3, 15, 9, 0), date(2027, 3, 15)),
    (date(2027, 3, 15), date(2027, 3, 15)),
    (None, None),
    ("", None),
    ("-", None),
    ("2027-02-30", None),       # 없는 날짜
    ("유통기한 없음", None),
    (20270315, date(2027, 3, 15)),  # 숫자로 적힌 YYYYMMDD
    (46461, None),                  # 엑셀 날짜 일련번호는 짐작하지 않는다
    ("2027-03-150", None),
])
def test_expiry_cell_is_read_as_date_or_none(value, expected):
    assert _to_expiry(value) == expected


def test_clean_returns_expiry_per_inventory_unit_without_changing_other_results():
    result = _clean([_unit("a", "2027-03-15 00:00:00.000 Asia/Seoul"), _unit("b", "-"), _unit("c", "엉뚱한 값")])

    expiry = {u.source_inventory_key: u.expiry_date for u in result.inventory_units}
    assert expiry == {"a": date(2027, 3, 15), "b": None, "c": None}
    assert result.validation.excluded_skus == []
    # 계약 JSON은 camelCase이고 날짜가 없으면 null이다.
    units = result.to_json_dict()["inventoryUnits"]
    assert {u["sourceInventoryKey"]: u["expiryDate"] for u in units} == {"a": "2027-03-15", "b": None, "c": None}


def test_ledger_without_expiry_column_still_cleans():
    raw = build_ledger([1, 2], [_unit("a", None)])
    # 유통기한 열 이름을 지운 파일도 지금처럼 정제된다.
    wb = openpyxl.load_workbook(BytesIO(raw))
    ws = wb.active
    for cell in ws[1]:
        if cell.value == "유통기한":
            cell.value = None
    buf = BytesIO()
    wb.save(buf)
    source = SourceFile(upload_file_id=1, storage_key="fake", original_name="2026-05.xlsx")

    result = clean([(source, buf.getvalue())], set())

    assert [u.expiry_date for u in result.inventory_units] == [None]
    assert read_ledger(buf.getvalue(), "2026-05.xlsx")["expiry_date"].isna().all()
