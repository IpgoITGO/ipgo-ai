"""DA-01 수불부 정제 (#12). 기대값은 tests/fixtures/da01_clean.md의 손계산 표에서 읽는다 (AI가 고치지 않음)."""
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.clean import check_stock_balance, read_ledger, to_product_daily
from tests.da01_ledger import FILES

WORKSHEET = Path(__file__).parent / "fixtures" / "da01_clean.md"
QTY_COLUMNS = ["opening_qty", "inbound_qty", "outbound_qty", "adjustment_qty", "closing_qty"]


def _section_rows(text: str, heading: str) -> list[list[str]]:
    """'## <heading>' 아래 첫 표의 데이터 행. 모든 칸이 빈 행은 뺀다."""
    body = text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    lines = [ln for ln in body.splitlines() if ln.startswith("|")][2:]  # 머리 행 · 구분 행 제외
    rows = [[c.strip() for c in ln.strip("|").split("|")] for ln in lines]
    return [r for r in rows if any(r)]


@pytest.fixture(scope="module")
def worksheet() -> str:
    text = WORKSHEET.read_text(encoding="utf-8")
    if not re.search(r"작성 상태:\s*완료", text):
        pytest.skip("da01_clean.md 손계산 기대값을 채우고 작성 상태를 '완료'로 바꾸면 실행")
    return text


@pytest.fixture(scope="module")
def units() -> pd.DataFrame:
    return pd.concat([read_ledger(content, name) for name, content in FILES], ignore_index=True)


def test_period(worksheet, units):
    [[start, end, as_of]] = _section_rows(worksheet, "3. 기대값 — 기간")
    product = to_product_daily(units)
    assert product["stock_date"].min() == date.fromisoformat(start)
    assert product["stock_date"].max() == date.fromisoformat(end)
    assert product["stock_date"].max() == date.fromisoformat(as_of)


def test_stock_balance_mismatches(worksheet, units):
    rows = _section_rows(worksheet, "4. 기대값 — 검산 불일치")
    expected = {(bc, date.fromisoformat(d), int(diff)) for bc, d, diff in rows}
    found = check_stock_balance(units)
    actual = {(r.sku_barcode, r.stock_date, int(r.diff_qty)) for r in found.itertuples()}
    assert actual == expected
    assert found["reason"].str.len().gt(0).all()


def test_product_daily(worksheet, units):
    rows = _section_rows(worksheet, "5. 기대값 — 상품 일별")
    expected = {(bc, date.fromisoformat(d)): tuple(int(v) for v in vals) for bc, d, *vals in rows}
    product = to_product_daily(units)
    actual = {
        (r.sku_barcode, r.stock_date): tuple(int(getattr(r, c)) for c in QTY_COLUMNS)
        for r in product.itertuples()
    }
    assert actual == expected
