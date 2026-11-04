"""SQS 결과 wire 제한을 유지하는 유한 크기 압축 계약(v2)."""
import base64
import gzip
import hashlib
import json

MAX_RESULT_BYTES = 900_000
MAX_DECODED_BYTES = 16 * 1024 * 1024


def encode_result(result) -> str:
    """작은 v1은 유지, 큰 v1만 gzip으로 감싼다. 한도 초과는 실패 결과로 전환할 호출자 책임."""
    raw = json.dumps(result.to_json_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError("결과 해제 크기 제한 초과")
    if len(raw) <= MAX_RESULT_BYTES:
        return raw.decode("utf-8")
    body = json.dumps(dict(contractVersion="2", encoding="gzip+base64", uncompressedBytes=len(raw),
                           sha256=hashlib.sha256(raw).hexdigest(),
                           payload=base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")),
                      separators=(",", ":"))
    if len(body.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("결과 wire 크기 제한 초과")
    return body
