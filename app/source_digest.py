"""자료 지문 v1의 정렬·JSON byte 계약을 유지하며 요청 안에서 scalar 변환을 재사용한다."""
import hashlib
import json
from datetime import date

import pandas as pd

_COLUMNS = frozenset({
    "product_id", "upload_file_id", "stock_date", "opening_qty", "inbound_qty", "outbound_qty",
    "adjustment_qty", "closing_qty", "is_valid_observation",
})
_MAX_SCALARS = 1024


class _ScalarEncoder:
    def __init__(self):
        self._integers = {}
        self._numbers = {}
        self._dates = {}

    @staticmethod
    def _remember(cache, key, encoded: bytes) -> bytes:
        if len(cache) >= _MAX_SCALARS:
            cache.clear()
        cache[key] = encoded
        return encoded

    def integer(self, value) -> bytes:
        number = int(value)
        encoded = self._integers.get(number)
        if encoded is None:
            encoded = self._remember(self._integers, number, b'"' + str(number).encode("ascii") + b'"')
        return encoded

    def number(self, value) -> bytes:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return b'"invalid"'
        encoded = self._numbers.get(number)
        if encoded is None:
            # float.hex 출력은 ASCII다. -0은 v1처럼 +0으로 정규화한다.
            encoded = b'"' + (0.0 if number == 0 else number).hex().encode("ascii") + b'"'
            encoded = self._remember(self._numbers, number, encoded)
        return encoded

    def day(self, value) -> bytes:
        if type(value) is date:
            encoded = self._dates.get(value)
            if encoded is not None:
                return encoded
        encoded = json.dumps(value.isoformat(), separators=(",", ":"), allow_nan=False).encode()
        return self._remember(self._dates, value, encoded) if type(value) is date else encoded

    @staticmethod
    def flag(value) -> bytes:
        return b"true" if bool(value) else b"false"


def source_digest(source_daily: pd.DataFrame) -> str:
    """v1: SHA256(컬럼 JSON+개행, 정렬된 행 JSON+개행). 수량은 float.hex 문자열이다."""
    columns = sorted(column for column in source_daily.columns if column in _COLUMNS)
    data = source_daily.sort_values(["product_id", "stock_date", "upload_file_id"])
    encoder = _ScalarEncoder()
    converters = [
        encoder.integer if column in ("product_id", "upload_file_id") else
        encoder.day if column == "stock_date" else
        encoder.flag if column == "is_valid_observation" else encoder.number
        for column in columns
    ]
    digest = hashlib.sha256(json.dumps(columns, separators=(",", ":")).encode() + b"\n")
    # scalar의 기존 JSON 표현을 연결하고 전체 자료의 두 번째 JSON 복사는 만들지 않는다.
    for row in data[columns].itertuples(index=False, name=None):
        encoded = b",".join(convert(value) for convert, value in zip(converters, row, strict=True))
        digest.update(b"[" + encoded + b"]\n")
    return digest.hexdigest()
