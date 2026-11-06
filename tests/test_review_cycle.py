"""Server92: 파일 내 입고 간격 제안. 기대값은 날짜 간격의 손계산이다."""
from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.clean import clean, estimate_review_cycle_days
from schemas.engine import CleanResponse, SourceFile
from tests.ledger_xlsx import build_ledger


def daily(groups):
    return pd.DataFrame([
        dict(sku_barcode=sku, stock_date=date(2026, 5, day), inbound_qty=qty,
             is_valid_observation=False)
        for sku, points in groups.items() for day, qty in points
    ], columns=["sku_barcode", "stock_date", "inbound_qty", "is_valid_observation"])


@pytest.mark.parametrize(("points", "expected"), [
    ([], None), ([(1, 1)], None), ([(1, 1), (8, 1)], None),
    ([(1, 1), (8, 1), (15, 1)], 7),
    ([(1, 1), (8, 1), (16, 1)], 8),  # 7.5 -> 8
    ([(1, 1), (1, 2), (8, 1)], None),
    ([(1, 1), (8, 0), (15, 1)], None),
])
def test_minimum_distinct_days_and_half_up(points, expected):
    assert estimate_review_cycle_days(daily({"A": points}), set()) == expected


def test_skus_are_not_interleaved_or_weighted_by_frequency():
    assert estimate_review_cycle_days(daily({"A": [(1, 1), (15, 1)],
                                            "B": [(8, 1), (22, 1)]}), set()) is None
    # A: median(2,2,2,2)=2, B: median(8,8)=8 -> 5. 모든 간격을 한꺼번에 평균내지 않는다.
    frame = daily({"A": [(d, 1) for d in [1, 3, 5, 7, 9]],
                   "B": [(d, 1) for d in [1, 9, 17]]})
    assert estimate_review_cycle_days(frame, set()) == 5
    assert estimate_review_cycle_days(frame, {"A"}) == 8
    assert estimate_review_cycle_days(frame, {"A", "B"}) is None


def test_real_clean_xlsx_populates_contract_including_first_inbound():
    unit = dict(barcode="A", name="상품", cost=10, mixed_id="LOT", received_on="2026-05-01",
                records={d: (10 if d in [1, 8, 15] else 0, 0, 0,
                             10 * (1 + (d >= 8) + (d >= 15))) for d in range(1, 16)})
    source = SourceFile(uploadFileId=7, storageKey="test", originalName="2026-05.xlsx")
    response = clean([(source, build_ledger(list(range(1, 16)), [unit]))], set())
    assert response.estimated_review_cycle_days == 7
    assert response.model_dump(by_alias=True)["estimatedReviewCycleDays"] == 7
    assert response.validation.excluded_skus == []
    assert response.product_daily_stocks[0].is_valid_observation is False


def test_excluded_sku_with_three_inbounds_is_not_estimated():
    unit = dict(barcode="A", name="상품", cost=10, mixed_id="LOT", received_on="2026-05-01",
                records={1: (10, 0, 0, 10), 8: (10, 0, 0, 999), 15: (10, 0, 0, 1009)})
    source = SourceFile(uploadFileId=7, storageKey="test", originalName="2026-05.xlsx")
    response = clean([(source, build_ledger(list(range(1, 16)), [unit]))], set())
    assert response.estimated_review_cycle_days is None
    assert len(response.validation.excluded_skus) == 1


@pytest.mark.parametrize("value", [0, -1, 1.5, "7", True])
def test_estimate_contract_rejects_non_positive_or_non_integer_values(value):
    body = dict(periodStart="2026-05-01", periodEnd="2026-05-31", asOfDate="2026-05-31",
                products=[], inventoryUnits=[], unitDailyRecords=[], productDailyStocks=[],
                validation=dict(checkedSkuCount=0, targetSkuCount=0, excludedSkus=[]),
                estimatedReviewCycleDays=value)
    with pytest.raises(ValidationError):
        CleanResponse.model_validate(body)
