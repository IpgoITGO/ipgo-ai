"""손계산 테스트 예시. 기대값은 사람이 직접 계산해 적는다 (AI가 고치지 않음)."""
import pytest

from engine.replenish import round_to_order_unit


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
