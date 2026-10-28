"""DA-02 유효 관측일 판정."""
import pandas as pd


def mark_valid_observations(product_daily: pd.DataFrame) -> pd.DataFrame:
    """is_valid_observation · observation_note 칼럼을 붙여 돌려준다.

    - 시작 재고(전일 종료 재고)가 0인 날 → 유효 관측일 아님 (당일 입고가 있어도 제외)
    - 종료 재고 0인 날 → 포함하되 표시
    - 첫 입고 이전 기간 → 품절과 구분해 "첫 입고 이전"
    """
    raise NotImplementedError("DA-02 mark_valid_observations")
