"""상품별 위치 조회와 기존 전체 자료 비교의 결과·원본 범위가 같은지 확인한다."""
from datetime import timedelta

import pandas as pd
import pytest

from engine.calculate import calculate
from schemas.base import RiskBasis
from tests.test_risk import START, calculation_case, context, sample


@pytest.mark.parametrize("selected", [None, [101, 999]])
@pytest.mark.parametrize("condition", ["clean", "duplicate", "missing_closing", "text_quantity", "invalid_flag"])
def test_product_positions_preserve_results_missing_products_and_pooled_source(selected, condition):
    request, own = calculation_case()
    peer = own.assign(product_id=202, outbound_qty=2)
    daily = pd.concat([peer, own], ignore_index=True)
    request.settings.extend([
        request.settings[0].model_copy(update={"product_id": product}) for product in (202, 999)
    ])
    request.product_ids = selected
    at = (daily.product_id == 101) & (daily.stock_date == request.as_of_date)
    if condition == "duplicate":
        daily = pd.concat([daily, daily[at]], ignore_index=True)
    elif condition == "missing_closing":
        daily = daily[~at]
    elif condition == "text_quantity":
        daily["outbound_qty"] = daily.outbound_qty.astype(str)
    elif condition == "invalid_flag":
        daily["is_valid_observation"] = daily.is_valid_observation.astype(object)
        daily.loc[at, "is_valid_observation"] = "false"
    # 다른 upload와 미래 기록은 기존 _inputs 경계에서 빠져야 한다.
    outside = own.iloc[:1].assign(upload_file_id=999, outbound_qty=-1)
    future = own.iloc[:1].assign(stock_date=request.as_of_date + timedelta(days=1), closing_qty=-1)
    daily = pd.concat([daily, outside, future], ignore_index=True).iloc[::-1]
    ctx = context(sample(), sample(0, product_id=202),
                  sample(2, product_id=202, origin=START + timedelta(days=1)))
    before = daily.copy(deep=True)
    # object ID는 정수 위치 index를 사용하지 않고 기존 == 비교 경로를 쓴다.
    reference = daily.assign(product_id=daily.product_id.astype(object))
    expected = calculate(request, reference, risk_context=ctx)
    actual = calculate(request, daily, risk_context=ctx)
    assert actual.to_json_dict() == expected.to_json_dict()
    pd.testing.assert_frame_equal(daily, before)
    assert 999 in {row.product_id for row in actual.results}
    if condition in ("clean", "text_quantity"):
        own_result = next(row for row in actual.results if row.product_id == 101)
        assert own_result.risk_basis == RiskBasis.POOLED
