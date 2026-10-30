"""DA-02 유효 관측일 판정."""
import pandas as pd

BEFORE_FIRST_RECEIPT = "첫 입고 이전"
NOT_OBSERVED = "시작재고 0(미관측)"
CLOSING_ZERO = "종료재고 0"


def mark_valid_observations(product_daily: pd.DataFrame) -> pd.DataFrame:
    """is_valid_observation · observation_note 칼럼을 붙여 돌려준다. 수량 칼럼은 바꾸지 않는다.

    - 시작 재고(전일 종료 재고)가 0인 날 → 유효 관측일 아님 (당일 입고가 있어도 제외)
    - 종료 재고 0인 날 → 포함하되 표시 (시작 재고도 0이면 위 규칙이 먼저)
    - 첫 입고 이전 기간 → 품절과 구분해 "첫 입고 이전". 자료 첫날부터 시작 재고가 0인 상품의
      첫 입고일(입고 > 0인 첫날)까지 [가정: 첫 입고일 포함, #21]
    """
    df = product_daily.copy()
    df["is_valid_observation"] = df["opening_qty"] > 0

    order = df.sort_values(["sku_barcode", "stock_date"])
    by_product = order.groupby("sku_barcode")
    starts_empty = by_product["opening_qty"].transform("first") == 0
    received_before = by_product["inbound_qty"].transform(lambda s: (s > 0).cumsum().shift(1, fill_value=0) > 0)
    before_first = (starts_empty & ~received_before).reindex(df.index)

    df["observation_note"] = None
    zero_open = df["opening_qty"] == 0
    df.loc[zero_open & before_first, "observation_note"] = BEFORE_FIRST_RECEIPT
    df.loc[zero_open & ~before_first, "observation_note"] = NOT_OBSERVED
    df.loc[~zero_open & (df["closing_qty"] == 0), "observation_note"] = CLOSING_ZERO
    return df
