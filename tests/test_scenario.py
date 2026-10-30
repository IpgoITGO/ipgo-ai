"""DA-06 시나리오 시뮬레이션 (#26). 입력 · 기대값은 fixtures/da06_simulate.md 손계산 표에서 읽는다 (AI가 고치지 않음)"""
import re
from pathlib import Path

import pytest

from engine.scenario import simulate

WORKSHEET = Path(__file__).parent / "fixtures" / "da06_simulate.md"


def _table(text: str, heading: str) -> list[list[str]]:
    """'## <heading>' 아래 첫 표의 데이터 행."""
    body = text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    lines = [ln for ln in body.splitlines() if ln.startswith("|")][2:]  # 머리 행 · 구분 행 제외
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in lines]


def _num(cell: str) -> float:
    return pytest.approx(float(cell), abs=0.01)


def _day(cell: str) -> int:
    """'s+3' → 3."""
    return int(cell.removeprefix("s+"))


@pytest.fixture(scope="module")
def worksheet() -> str:
    text = WORKSHEET.read_text(encoding="utf-8")
    if not re.search(r"작성 상태:\s*완료", text):
        pytest.skip("da06_simulate.md 손계산 기대값을 채우고 작성 상태를 '완료'로 바꾸면 실행")
    return text


@pytest.fixture(scope="module")
def results(worksheet):
    out = {}
    for case, start, demand, horizon, lead, qty, planned in _table(worksheet, "1. 입력"):
        receipts = {}
        if planned != "없음":
            for item in planned.split(","):
                day, amount = item.split(":")
                receipts[_day(day.strip())] = float(amount)
        out[case] = simulate(float(start), [float(demand)] * int(horizon), int(qty), int(lead), receipts)
    return out


def test_daily(worksheet, results):
    for case, day, _, unmet, closing in _table(worksheet, "3. 기대값 — 날짜별"):
        r, k = results[case], _day(day) - 1
        assert (r.daily_unmet[k], r.daily_closing[k]) == (_num(unmet), _num(closing)), (case, day)


def test_summary(worksheet, results):
    for case, depletion, ending, pre, post in _table(worksheet, "4. 기대값 — 요약"):
        r = results[case]
        assert r.depletion_day == (None if depletion == "없음" else _day(depletion)), case
        actual = (r.ending_stock, r.pre_arrival_unmet, r.post_arrival_unmet)
        assert actual == (_num(ending), _num(pre), _num(post)), case
