"""DA-02 유효 관측일 판정 (#21). 입력 · 기대값은 fixtures/da02_observation.md 손계산 표에서 읽는다 (AI가 고치지 않음)"""
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.observation import mark_valid_observations

WORKSHEET = Path(__file__).parent / "fixtures" / "da02_observation.md"
QTY_COLUMNS = ["opening_qty", "inbound_qty", "outbound_qty", "adjustment_qty", "closing_qty"]


@pytest.fixture(scope="module")
def rows() -> list[list[str]]:
    text = WORKSHEET.read_text(encoding="utf-8")
    if not re.search(r"작성 상태:\s*완료", text):
        pytest.skip("da02_observation.md 손계산 기대값을 채우고 작성 상태를 '완료'로 바꾸면 실행")
    body = text.split("## 2. 기대값", 1)[1].split("\n## ", 1)[0]
    lines = [ln for ln in body.splitlines() if ln.startswith("|")][2:]  # 머리 행 · 구분 행 제외
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in lines]


def test_mark_valid_observations(rows):
    product_daily = pd.DataFrame(
        [(bc, date.fromisoformat(d), *(int(v) for v in qty)) for bc, d, *qty, _, _ in rows],
        columns=["sku_barcode", "stock_date", *QTY_COLUMNS],
    )
    expected = {(bc, date.fromisoformat(d)): (valid == "O", note or None) for bc, d, *_, valid, note in rows}

    result = mark_valid_observations(product_daily)
    actual = {
        (r.sku_barcode, r.stock_date): (bool(r.is_valid_observation), r.observation_note)
        for r in result.itertuples()
    }
    assert actual == expected
    assert result[QTY_COLUMNS].equals(product_daily[QTY_COLUMNS])  # 수량은 바꾸지 않음
