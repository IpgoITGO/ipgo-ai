import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import lifespan, worker
from app.config import get_settings
from app.jobs import JobStatus, JobType, ResultEnvelope
from app.main import app
from tests.payloads import CALC_REQUEST


def envelope(payload=CALC_REQUEST):
    return json.dumps({"contractVersion": "1", "jobId": "test", "jobType": "CALCULATE", "payload": payload})


class Queue:
    def __init__(self, body, fail_send=False):
        self.body = body
        self.fail_send = fail_send
        self.calls = []
        self.stop = threading.Event()

    def receive_message(self, **kwargs):
        self.calls.append(("receive", kwargs))
        self.stop.set()
        return {"Messages": [{"Body": self.body, "ReceiptHandle": "test"}]}

    def send_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        if self.fail_send:
            raise RuntimeError("secret")

    def delete_message(self, **kwargs):
        self.calls.append(("delete", kwargs))


def run_message(monkeypatch, body, fail_send=False):
    queue = Queue(body, fail_send)
    # Stop after processing, not after receiving.
    stop = threading.Event()
    original_send = queue.send_message

    def send(**kwargs):
        try:
            original_send(**kwargs)
        finally:
            stop.set()

    queue.send_message = send
    original_receive = queue.receive_message

    def receive(**kwargs):
        if queue.stop.is_set():
            stop.set()
            return {}
        return original_receive(**kwargs)

    queue.receive_message = receive
    worker.run_loop(stop, SimpleNamespace(jobs_queue_url="jobs", results_queue_url="results"), queue)
    return queue.calls


def test_defaults_and_queue_required(monkeypatch):
    monkeypatch.delenv("ENGINE_MODE", raising=False)
    monkeypatch.delenv("IPGO_EMBEDDED_WORKER_ENABLED", raising=False)
    monkeypatch.delenv("IPGO_JOBS_QUEUE_URL", raising=False)
    assert get_settings().engine_mode == "mock"
    assert not get_settings().embedded_worker_enabled
    with pytest.raises(RuntimeError):
        worker.create_worker()


def test_send_before_delete_and_failed_send_preserves_message(monkeypatch):
    monkeypatch.setattr(worker, "execute", lambda job: ResultEnvelope(job_id=job.job_id, job_type=job.job_type,
                                                                      status=JobStatus.SUCCEEDED))
    assert [x[0] for x in run_message(monkeypatch, envelope())] == ["receive", "send", "delete"]
    assert [x[0] for x in run_message(monkeypatch, envelope(), True)] == ["receive", "send"]


def test_invalid_payload_and_oversized_utf8(monkeypatch):
    calls = run_message(monkeypatch, envelope({}))
    assert json.loads(calls[1][1]["MessageBody"])["error"]["code"] == "INVALID_INPUT"
    assert calls[-1][0] == "delete"
    monkeypatch.setattr(worker, "execute", lambda job: ResultEnvelope(job_id=job.job_id, job_type=JobType.CALCULATE,
                        status=JobStatus.SUCCEEDED, result={"text": "한" * 310_000}))
    calls = run_message(monkeypatch, envelope())
    body = calls[1][1]["MessageBody"]
    assert json.loads(body)["contractVersion"] == "2"
    import base64
    import gzip
    decoded = json.loads(gzip.decompress(base64.b64decode(json.loads(body)["payload"])))
    assert decoded["result"]["text"] == "한" * 310_000
    assert len(body.encode()) < 900_000


def test_malformed_envelope_goes_to_dlq_without_logging_body(monkeypatch, caplog):
    calls = run_message(monkeypatch, "secret-invalid")
    assert [x[0] for x in calls] == ["receive"]
    assert "secret-invalid" not in caplog.text


def test_lifespan_worker_starts_stops_without_signals(monkeypatch):
    monkeypatch.setenv("IPGO_EMBEDDED_WORKER_ENABLED", "true")
    monkeypatch.setattr(lifespan, "create_worker", lambda: (object(), object()))
    started = threading.Event()
    stopped = threading.Event()

    def loop(stop, *_args):
        started.set()
        stop.wait(3)
        stopped.set()

    monkeypatch.setattr(lifespan, "run_loop", loop)
    monkeypatch.setattr(worker.signal, "signal", lambda *_: pytest.fail("signal used"))
    with TestClient(app) as client:
        assert started.wait(1)
        assert client.get("/health").status_code == 200
        assert app.state.worker_thread.is_alive()
    assert stopped.is_set()
    assert app.state.worker_thread is None


def test_lifespan_failure_and_dead_worker_health(monkeypatch):
    monkeypatch.setenv("IPGO_EMBEDDED_WORKER_ENABLED", "true")
    monkeypatch.delenv("IPGO_JOBS_QUEUE_URL", raising=False)
    with pytest.raises(RuntimeError), TestClient(app):
        pass
    monkeypatch.setattr(lifespan, "create_worker", lambda: (object(), object()))
    monkeypatch.setattr(lifespan, "run_loop", lambda *_: (_ for _ in ()).throw(RuntimeError("secret")))
    with TestClient(app) as client:
        for _ in range(100):
            if not app.state.worker_thread.is_alive():
                break
            time.sleep(0.01)
        assert client.get("/health").status_code == 503
