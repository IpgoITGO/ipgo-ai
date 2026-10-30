import json
from types import SimpleNamespace

import pandas as pd

from app import jobs, repository, storage
from app.jobs import JobStatus, JobType
from app.mocks import load_mock
from app.worker import process_message
from schemas.engine import CalculateResponse, SourceFile
from tests.payloads import CALC_REQUEST


def test_local_storage_reader_stays_inside_configured_root(tmp_path, monkeypatch):
    original = tmp_path / "1" / "upload" / "original.xlsx"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"xlsx")
    monkeypatch.setenv("STORAGE_BASE", str(tmp_path))

    content = storage.read_source_file(
        SourceFile(uploadFileId=1, storageKey="1/upload", originalName="ledger.xlsx")
    )

    assert content == b"xlsx"


def test_s3_storage_reader_uses_bucket_and_original_key(monkeypatch):
    calls = []

    class Body:
        def read(self, *_args):
            return b"from-s3"

    class S3:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {"Body": Body(), "ContentLength": 7}

    monkeypatch.setenv("IPGO_UPLOAD_BUCKET", "private-upload-bucket")
    monkeypatch.setattr(storage.boto3, "client", lambda *_args, **_kwargs: S3())

    content = storage.read_source_file(
        SourceFile(uploadFileId=7, storageKey="3/key", originalName="ledger.xlsx")
    )

    assert content == b"from-s3"
    assert calls == [{"Bucket": "private-upload-bucket", "Key": "3/key/original.xlsx"}]


def test_worker_returns_contract_failure_for_unimplemented_engine(monkeypatch):
    monkeypatch.setattr(jobs.repository, "load_product_daily", lambda *_: pd.DataFrame())
    monkeypatch.setattr(jobs.calculation, "calculate", lambda *_: (_ for _ in ()).throw(NotImplementedError()))
    body = json.dumps(
        {
            "contractVersion": "1",
            "jobId": "run-34",
            "jobType": JobType.CALCULATE,
            "payload": CALC_REQUEST,
        }
    )

    result = process_message(body)

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.code == "NOT_IMPLEMENTED"


def test_job_dispatch_returns_camel_case_result(monkeypatch):
    monkeypatch.setattr(jobs.repository, "load_product_daily", lambda *_: pd.DataFrame())
    monkeypatch.setattr(jobs.calculation, "calculate", lambda *_: load_mock("calculate", CalculateResponse))
    envelope = jobs.JobEnvelope.model_validate(
        {
            "contractVersion": "1",
            "jobId": "run-34",
            "jobType": "CALCULATE",
            "payload": CALC_REQUEST,
        }
    )

    result = jobs.execute(envelope).to_json_dict()

    assert result["status"] == "SUCCEEDED"
    assert result["result"]["engineVersion"]


def test_database_reader_uses_read_only_connection_and_source_filter(monkeypatch):
    executed = []

    class Cursor:
        description = [SimpleNamespace(name="product_id"), SimpleNamespace(name="closing_qty")]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params):
            executed.append((query, params))

        def fetchall(self):
            return [(11, 3.5)]

    class Connection:
        read_only = False
        rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def rollback(self):
            self.rolled_back = True

    connection = Connection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://reader@database/ipgo")
    monkeypatch.setattr(repository.psycopg, "connect", lambda *_args, **_kwargs: connection)

    result = repository.load_product_daily(3, [21, 22])

    assert connection.read_only is True
    assert connection.rolled_back is True
    assert executed[0][1] == (3, [21, 22])
    assert result.to_dict(orient="records") == [{"product_id": 11, "closing_qty": 3.5}]
