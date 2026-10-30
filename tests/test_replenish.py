"""손계산 테스트 예시. 기대값은 사람이 직접 계산해 적는다 (AI가 고치지 않음)."""
import re
from datetime import date
from pathlib import Path

import pytest

from engine.replenish import required_qty, round_to_order_unit, split_forecast, split_planned_receipts, start_stock

WORKSHEET = Path(__file__).parent / "fixtures" / "da04_replenish.md"


@pytest.mark.parametrize(
    ("qty", "unit", "expected"),
    [
        (66.66, 12, 72),   # 66.66 / 12 = 5.55 → 6묶음 → 72개
        (72.0, 12, 72),    # 딱 맞으면 그대로
        (0.0, 12, 0),      # 필요 없음
        (-3.0, 12, 0),     # 음수는 0
        (0.1, 1, 1),       # 입고 단위 1개
    ],
)
def test_round_to_order_unit(qty, unit, expected):
    assert round_to_order_unit(qty, unit) == expected


def _table(text: str, heading: str) -> list[list[str]]:
    """'## <heading>' 아래 첫 표의 데이터 행."""
    body = text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    lines = [ln for ln in body.splitlines() if ln.startswith("|")][2:]  # 머리 행 · 구분 행 제외
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in lines]


def _num(cell: str) -> float:
    return pytest.approx(float(cell), abs=0.01)


@pytest.fixture(scope="module")
def worksheet() -> str:
    text = WORKSHEET.read_text(encoding="utf-8")
    if not re.search(r"작성 상태:\s*완료", text):
        pytest.skip("da04_replenish.md 손계산 기대값을 채우고 작성 상태를 '완료'로 바꾸면 실행")
    return text


def test_da04_worksheet(worksheet):
    """DA-04 (#28): 예측 나누기 → 기존 발주 나누기 → 시작 재고 → 올림 전 필요량 → 권장 수량."""
    expected = {row[0]: row[1:] for row in _table(worksheet, "3. 기대값")}
    for case, t, s, horizon, total, mult, closing, unit, unmet, planned in _table(worksheet, "1. 입력"):
        as_of, today, horizon = date.fromisoformat(t), date.fromisoformat(s), int(horizon)
        receipts = [] if planned == "없음" else [
            (date.fromisoformat(d.strip()), float(q)) for d, q in (item.rsplit(":", 1) for item in planned.split(","))
        ]
        gap = (today - as_of).days
        daily, cover = split_forecast(float(total), gap, horizon)
        before_start, in_horizon = split_planned_receipts(receipts, as_of, today, horizon)
        stock = start_stock(float(closing), daily, gap, before_start)
        planned_qty = sum(in_horizon.values())
        need = required_qty(cover, float(mult), stock, planned_qty, float(unmet))

        e_daily, e_cover, e_stock, e_estimated, e_planned, e_need, e_qty = expected[case]
        assert (daily, cover, stock) == (_num(e_daily), _num(e_cover), _num(e_stock)), case
        assert (gap > 0) == (e_estimated == "O"), case
        assert (planned_qty, need) == (_num(e_planned), _num(e_need)), case
        assert round_to_order_unit(need, int(unit)) == int(e_qty), case
