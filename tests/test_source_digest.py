"""지문 v1의 독립 JSON 직렬화 기준과 최적화 byte 계약을 비교한다."""
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from app.source_digest import source_digest


def reference_digest(daily):
    columns = sorted(column for column in daily.columns if column in {
        "product_id", "upload_file_id", "stock_date", "opening_qty", "inbound_qty", "outbound_qty",
        "adjustment_qty", "closing_qty", "is_valid_observation",
    })
    digest = hashlib.sha256(json.dumps(columns, separators=(",", ":")).encode() + b"\n")
    for row in daily.sort_values(["product_id", "stock_date", "upload_file_id"])[columns].itertuples(
        index=False, name=None,
    ):
        values = []
        for key, value in zip(columns, row, strict=True):
            if key in ("product_id", "upload_file_id"):
                values.append(str(int(value)))
            elif key == "stock_date":
                values.append(value.isoformat())
            elif key == "is_valid_observation":
                values.append(bool(value))
            else:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    values.append("invalid")
                    continue
                values.append((0.0 if number == 0 else number).hex())
        digest.update(json.dumps(values, separators=(",", ":"), allow_nan=False).encode() + b"\n")
    return digest.hexdigest()


def frame(value=0):
    return pd.DataFrame([{
        "product_id": "١٠١", "upload_file_id": "0021", "stock_date": date(2024, 2, 29),
        "opening_qty": value, "inbound_qty": value, "outbound_qty": value,
        "adjustment_qty": value, "closing_qty": value, "is_valid_observation": True,
        "ignored_note": '한글·따옴표"·역슬래시\\·줄바꿈\n',
    }], dtype=object)


@pytest.mark.parametrize("value", [
    0, -0.0, 1, -1, 1.25, 1e-310, 1e308, float("nan"), float("inf"), -float("inf"),
    None, "1.25", "NaN", "invalid\n한글", [], {}, True, False, Decimal("1.1"), Decimal("sNaN"),
])
def test_numeric_scalar_encodings_match_v1(value):
    daily = frame(value)
    assert source_digest(daily) == reference_digest(daily)


@pytest.mark.parametrize("flag", [True, False, 0, 1, "", "false", None])
def test_boolean_conversion_matches_v1_even_for_invalid_observation_flags(flag):
    daily = frame()
    daily["is_valid_observation"] = pd.Series([flag], dtype=object)
    assert source_digest(daily) == reference_digest(daily)


def test_cache_eviction_column_order_missing_optional_columns_and_empty_input():
    daily = pd.DataFrame([{
        "product_id": n, "upload_file_id": 21, "stock_date": date(2020, 1, 1) + timedelta(days=n),
        "outbound_qty": n / 13, "closing_qty": -0.0, "is_valid_observation": n % 2 == 0,
    } for n in range(1300)])
    before = daily.copy(deep=True)
    for data in (daily, daily.iloc[::-1, ::-1], daily.iloc[:0]):
        assert source_digest(data) == reference_digest(data)
    pd.testing.assert_frame_equal(daily, before)


def test_requests_do_not_reuse_stale_values_and_signed_zero_is_canonical():
    first, second = frame(-0.0), frame(0.0)
    assert source_digest(first) == source_digest(second) == reference_digest(first)
    changed = frame(2.5)
    assert source_digest(changed) == reference_digest(changed) != source_digest(first)


@pytest.mark.parametrize("field,value,error", [
    ("outbound_qty", 10**1000, OverflowError),
    ("product_id", "invalid", ValueError),
    ("stock_date", None, AttributeError),
    ("is_valid_observation", pd.NA, TypeError),
])
def test_nonrecoverable_scalar_errors_are_preserved(field, value, error):
    daily = frame()
    daily[field] = pd.Series([value], dtype=object)
    for make_digest in (reference_digest, source_digest):
        with pytest.raises(error):
            make_digest(daily)
