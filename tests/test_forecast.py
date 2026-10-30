"""DA-03 출고량 예측 (#23). 입력 · 기대값은 fixtures/da03_forecast.md 손계산 표에서 읽는다 (AI가 고치지 않음)"""
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.forecast import forecast_a, forecast_b

WORKSHEET = Path(__file__).parent / "fixtures" / "da03_forecast.md"


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
        pytest.skip("da03_forecast.md 손계산 기대값을 채우고 작성 상태를 '완료'로 바꾸면 실행")
    return text


@pytest.fixture(scope="module")
def condition(worksheet) -> tuple[date, int]:
    [[as_of, days]] = _table(worksheet, "1. 조건")
    return date.fromisoformat(as_of), int(days)


@pytest.fixture(scope="module")
def daily(worksheet) -> dict[str, pd.DataFrame]:
    """구간 표를 하루 1행으로 펼친다."""
    rows = []
    for bc, start, end, qty, valid, _ in _table(worksheet, "2. 입력"):
        day, last = date.fromisoformat(start), date.fromisoformat(end)
        while day <= last:
            rows.append((bc, day, int(qty), valid == "O"))
            day += timedelta(days=1)
    df = pd.DataFrame(rows, columns=["sku_barcode", "stock_date", "outbound_qty", "is_valid_observation"])
    return {bc: g.reset_index(drop=True) for bc, g in df.groupby("sku_barcode")}


def test_forecast_b(worksheet, condition, daily):
    as_of, days = condition
    for bc, valid_days, total, mean, qty, unusable in _table(worksheet, "4. 기대값 — 기준 B"):
        result = forecast_b(daily[bc], as_of, days)
        if unusable == "O":
            assert result.qty is None and result.reason, bc
            continue
        assert (result.valid_days, result.valid_outbound) == (int(valid_days), _num(total)), bc
        assert (result.daily_mean, result.qty) == (_num(mean), _num(qty)), bc


def test_forecast_a(worksheet, condition, daily):
    as_of, days = condition
    for bc, bounds, p_h, c_90, p_90, trend, qty, unusable in _table(worksheet, "5. 기대값 — 계산식 A"):
        trend_bounds = None if bounds == "없음" else tuple(float(v) for v in bounds.split("~"))
        result = forecast_a(daily[bc], as_of, days, trend_bounds)
        if unusable == "O":
            assert result.qty is None and result.reason, bc
            continue
        assert (result.p_h, result.c_90, result.p_90) == (_num(p_h), _num(c_90), _num(p_90)), bc
        assert (result.trend, result.qty) == (_num(trend), _num(qty)), bc
