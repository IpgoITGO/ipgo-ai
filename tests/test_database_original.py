"""DB 원본 권한 필터·무결성·경로 분기를 가짜 DB와 합성 파일로 검증한다."""
import hashlib
from types import SimpleNamespace

import pytest

from app import jobs, repository
from app.errors import AppError
from engine.clean import CleaningInputError
from tests.ledger_xlsx import build_ledger
from tests.test_clean_connection import UNIT, Connection

SOURCE = SimpleNamespace(upload_file_id=7, storage_key="db/3/test", original_name="2026-05.xlsx")
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class Cursor:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.row


def setup(monkeypatch, row):
    cursor = Cursor(row)
    connection = Connection(cursor)
    monkeypatch.setenv("DATABASE_URL", "postgresql://reader@fake/ipgo")
    monkeypatch.setattr(repository.psycopg, "connect", lambda *_a, **_kw: connection)
    return connection, cursor


def test_bounded_readonly_source_filter_and_integrity(monkeypatch):
    content = b"synthetic-source"
    row = (len(content), hashlib.sha256(content).hexdigest(), MIME, memoryview(content))
    connection, cursor = setup(monkeypatch, row)
    assert repository.load_upload_original(3, 4, SOURCE) == content
    assert connection.read_only and connection.rolled_back
    query, params = cursor.queries[-1]
    assert "status = 'PROCESSING'" in query and "CASE WHEN size_bytes" in query
    assert params[1:] == (7, 3, 4, "db/3/test", "2026-05.xlsx")


@pytest.mark.parametrize("row", [None, (2, "wrong", MIME, b"ab"), (3, "wrong", MIME, b"ab"),
                                 (10485761, "wrong", MIME, None), (2, "wrong", "text/plain", b"ab")])
def test_invalid_or_mismatched_database_source_is_permanent_failure(monkeypatch, row):
    connection, _ = setup(monkeypatch, row)
    with pytest.raises(CleaningInputError):
        repository.load_upload_original(3, 4, SOURCE)
    assert connection.rolled_back


def test_missing_database_does_not_read_external_source(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(AppError):
        repository.load_upload_original(3, 4, SOURCE)


def test_clean_job_db_route_uses_real_cleaning_without_local_or_s3(monkeypatch):
    payload = {"uploadId": 7, "userId": 3, "warehouseId": 4, "previousUploadFileId": None,
               "files": [{"uploadFileId": 7, "storageKey": SOURCE.storage_key, "originalName": SOURCE.original_name}]}
    content = build_ledger([1, 2], [UNIT])
    monkeypatch.setattr(repository, "load_cleaning_context", lambda *_: (set(), {}, None))
    calls = []

    def original(user, warehouse, source):
        calls.append((user, warehouse, source.upload_file_id))
        return content

    monkeypatch.setattr(repository, "load_upload_original", original)
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda *_: pytest.fail("external storage used"))
    result = jobs.execute(jobs.JobEnvelope(contractVersion="1", jobId="db-test", jobType="CLEAN", payload=payload))
    assert result.status == "SUCCEEDED" and calls == [(3, 4, 7)]
    assert len(result.result["productDailyStocks"]) == 2
