from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.model19 import Bundle, Model19Error


class Quantile:
    def __init__(self, value, captured):
        self.value, self.captured = value, captured

    def predict(self, frame, **kwargs):
        self.captured.append(frame.copy())
        return np.array([self.value])


def fixture_bundle(count=5):
    b = Bundle.__new__(Bundle)
    b.cutoff = date(2026, 7, 31)
    b.mapping, b.counts = {"SYNTHETIC":1}, {1:count}
    b.pooled, b.group_errors, b.sku_errors = 4., {"SHORT_HISTORY":4.}, {1:(2,1.)}
    captured = []
    b.models = [Quantile(q, captured) for q in (2.,3.,4.)]
    day = date(2026, 11, 10)
    daily = pd.DataFrame([dict(product_id=999, stock_date=day-timedelta(days=i),
                               outbound_qty=2., is_valid_observation=True) for i in range(10)])
    return b, daily, day, captured


def test_threshold_99_100_preserves_original_and_uses_training_id():
    b, daily, day, captured = fixture_bundle()
    daily.loc[0,"outbound_qty"], daily.loc[1,"outbound_qty"] = 100., 99.
    original = daily.copy(deep=True)
    prediction = b.predict(daily,"SYNTHETIC",day)
    pd.testing.assert_frame_equal(daily,original)
    assert captured[0].product_id.iloc[0] == 1  # 서비스ID999를 학습ID1로 연결
    assert captured[0].mean_28.iloc[0] == pytest.approx((99+8*2)/10)
    assert prediction.buffer == pytest.approx(3.5)  # (2/12)*1 + (10/12)*4
    assert prediction.target == pytest.approx(4.8)  # q80+buffer=6.5, q90*1.2 상한


def test_new_products_and_short_training_history_are_different_cases():
    b,daily,day,_ = fixture_bundle(count=4)
    result = b.predict(daily,"SYNTHETIC",day)
    assert result.fallback and result.p50 == result.p80 == result.p90 == 38
    with pytest.raises(Model19Error,match="대응 정보"):
        b.predict(daily,"UNKNOWN",day)


def test_missing_gap_is_not_zero_sales_and_future_rows_do_not_leak():
    b,daily,day,captured = fixture_bundle()
    b.predict(daily,"SYNTHETIC",day+timedelta(days=2))
    assert captured[0].valid_days_28.iloc[0] == 10
    assert captured[0].mean_28.iloc[0] == 2
    assert captured[0].weekday.iloc[0] == (day+timedelta(days=2)).weekday()
    b.predict(pd.concat([daily,pd.DataFrame([dict(product_id=999,stock_date=day+timedelta(days=3),
                                                  outbound_qty=70.,is_valid_observation=True)])]),
              "SYNTHETIC",day+timedelta(days=2))
    pd.testing.assert_frame_equal(captured[0],captured[3])


def test_cutoff_and_insufficient_valid_history_fail_closed():
    b,daily,day,_ = fixture_bundle()
    with pytest.raises(Model19Error):
        b.predict(daily,"SYNTHETIC",date(2026,7,1))
    daily.loc[0:4,"is_valid_observation"] = False
    with pytest.raises(Model19Error,match="유효 기록"):
        b.predict(daily,"SYNTHETIC",day)
