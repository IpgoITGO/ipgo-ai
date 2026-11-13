"""DA-01 로컬 설정·파일→CLEAN→결과 발행의 오프라인 검증. 실제 DB/브로커 시험은 아니다."""
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app import repository, worker
from app.config import get_settings
from tests.da01_ledger import FILES

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/local-worker.env.example"


@pytest.fixture
def offline(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("오프라인 검증 중 외부 연결 시도")

    monkeypatch.setattr(socket.socket, "connect", deny)
    for name in list(os.environ):
        if name.startswith(("AWS_", "IPGO_", "OPENAI_")) or name in {"DATABASE_URL", "STORAGE_BASE"}:
            monkeypatch.delenv(name)
    for name, value in dotenv_values(EXAMPLE).items():
        monkeypatch.setenv(name, value)


def test_local_endpoint_is_selected_without_contacting_broker(offline, monkeypatch):
    endpoint = "http://127.0.0.1:9324"
    monkeypatch.setenv("IPGO_JOBS_QUEUE_URL", endpoint + "/queue/test-jobs")
    monkeypatch.setenv("IPGO_RESULTS_QUEUE_URL", endpoint + "/queue/test-results")
    settings, sqs = worker.create_worker()
    try:
        assert sqs.meta.endpoint_url == endpoint
        assert settings.jobs_queue_url != settings.results_queue_url
        assert not settings.embedded_worker_enabled
    finally:
        sqs.close()


def test_explicit_file_does_not_import_ambient_environment_or_dotenv(tmp_path):
    # 문서의 env -i + dotenv CLI + 자동 .env 차단을 서버 기동 없이 실행한다.
    (tmp_path / ".env").write_text("OPENAI_API_KEY=fixture-must-not-load\nAWS_SESSION_TOKEN=fixture\n")
    config = tmp_path / ".env.local-worker"
    config.write_text(EXAMPLE.read_text())
    code = """
import os, socket
def deny(*a, **kw):
    raise AssertionError('network forbidden')
socket.socket.connect = deny
from app.config import get_settings
s = get_settings()
assert s.engine_mode == 'real' and not s.embedded_worker_enabled
assert s.database_url is None and s.upload_bucket is None
assert os.environ['AWS_ENDPOINT_URL_SQS'] == 'http://127.0.0.1:9324'
assert os.environ['OPENAI_API_KEY'] == ''
assert 'AWS_SESSION_TOKEN' not in os.environ and 'AWS_PROFILE' not in os.environ
"""
    # env에 PATH와 차단 flag만 넘기는 것이 env -i와 같은 경계다.
    result = subprocess.run(
        [sys.executable, "-m", "dotenv", "-f", str(config), "run", "--override", "--",
         sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r});\n" + code],
        cwd=tmp_path, env={"PATH": os.environ["PATH"], "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


class SingleJobQueue:
    """로컬 브로커를 대신한다. 메시지 발행/삭제 순서만 관측하며 Server 저장을 흉내내지 않는다."""

    def __init__(self, body, stop, fail_send):
        self.body, self.stop, self.fail_send = body, stop, fail_send
        self.calls = []
        self.published = []

    def receive_message(self, **kwargs):
        if self.calls:
            self.stop.set()
            return {}
        self.calls.append(("receive", kwargs["QueueUrl"]))
        return {"Messages": [{"Body": self.body, "ReceiptHandle": "fixture-receipt"}]}

    def send_message(self, **kwargs):
        self.calls.append(("send", kwargs["QueueUrl"]))
        self.stop.set()
        if self.fail_send:
            raise RuntimeError("fixture send failure")
        self.published.append(json.loads(kwargs["MessageBody"]))

    def delete_message(self, **kwargs):
        self.calls.append(("delete", kwargs["QueueUrl"]))
        assert kwargs["ReceiptHandle"] == "fixture-receipt"


@pytest.mark.parametrize("invalid,fail_send", [(False, False), (True, False), (False, True)])
def test_local_file_clean_result_and_failed_publish_preserves_job(
    offline, monkeypatch, tmp_path, invalid, fail_send,
):
    name, content = FILES[0]  # 기존 손계산 fixture 사용. 계산 입력·기대값은 변경하지 않는다.
    key = "3/fixture-upload"
    source = tmp_path / key / "original.xlsx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"invalid-xlsx" if invalid else content)
    monkeypatch.setenv("STORAGE_BASE", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture@localhost/test")
    monkeypatch.setenv("IPGO_JOBS_QUEUE_URL", "local-jobs")
    monkeypatch.setenv("IPGO_RESULTS_QUEUE_URL", "local-results")
    contexts = []

    def context(user_id, warehouse_id, upload_id, previous_id, file):
        contexts.append((user_id, warehouse_id, upload_id, previous_id, file.storage_key))
        return set(), {}, None  # DB 문맥만 대체한다. storage·정제·worker는 실제 코드다.

    monkeypatch.setattr(repository, "load_cleaning_context", context)
    body = json.dumps({"contractVersion": "1", "jobId": "local-clean-fixture", "jobType": "CLEAN",
                       "payload": {"uploadId": 7, "userId": 3, "warehouseId": 4,
                                   "files": [{"uploadFileId": 7, "storageKey": key, "originalName": name}]}})
    stop = threading.Event()
    queue = SingleJobQueue(body, stop, fail_send)
    worker.run_loop(stop, get_settings(), queue)
    assert contexts == [(3, 4, 7, None, key)]
    expected = [("receive", "local-jobs"), ("send", "local-results")]
    assert queue.calls == expected + ([] if fail_send else [("delete", "local-jobs")])
    if fail_send:
        assert not queue.published
    else:
        [result] = queue.published
        assert result["jobId"] == "local-clean-fixture" and result["jobType"] == "CLEAN"
        if invalid:
            assert result["status"] == "FAILED" and result["error"]["code"] == "INVALID_SOURCE"
            assert result["error"]["message"]
        else:
            assert result["status"] == "SUCCEEDED" and result["result"]["products"]
