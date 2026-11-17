"""#121: 가짜 큐/DB와 고정 시계로 계측 의미·비밀값 비노출·업무 불변을 검증한다."""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app import calculation_timing as timing
from app import repository, worker
from app.config import get_settings
from app.jobs import JobEnvelope, JobStatus, ResultEnvelope
from tests.payloads import CALC_REQUEST

JOB_ID = "01234567-89ab-4cde-8fab-0123456789ab"
ATTRIBUTES = {"SentTimestamp": "100", "ApproximateFirstReceiveTimestamp": "250", "ApproximateReceiveCount": "1"}


def job(job_id=JOB_ID, payload=None, job_type="CALCULATE"):
    return JobEnvelope(contractVersion="1", jobId=job_id, jobType=job_type,
                       payload=CALC_REQUEST if payload is None else payload)


@pytest.fixture
def records(monkeypatch):
    values = []
    logger = logging.Logger("fixture.calculation_timing")
    monkeypatch.setattr(timing, "LOGGER", logger)
    monkeypatch.setattr(logger, "info", lambda value: values.append(json.loads(value)))
    return values


def test_settings_are_explicitly_opt_in(monkeypatch):
    monkeypatch.delenv("IPGO_ACCEPTANCE_TIMING_ENABLED", raising=False)
    assert not get_settings().acceptance_timing_enabled
    monkeypatch.setenv("IPGO_ACCEPTANCE_TIMING_ENABLED", "true")
    assert get_settings().acceptance_timing_enabled


def test_monotonic_database_elapsed_and_first_delivery_are_distinct(monkeypatch, records):
    monotonic = iter([0, 1_000_000, 6_000_000, 7_000_000])
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: next(monotonic))
    monkeypatch.setattr(timing.time, "time_ns", lambda: 1_000_000_000)
    with timing.attempt(ATTRIBUTES, enabled=True):
        timing.identify(job())
        timing.database_read_timed(lambda: None)()
    record, = records
    assert record["databaseRead"] == {"elapsedMs": 5.0, "status": "success"}
    assert record["attemptElapsedMs"] == 7.0
    assert record["approxFirstQueueWaitMs"] == record["currentReceiveQueueWaitMs"] == 150
    assert record["messageAgeAtReceiveMs"] == 900
    assert record["redeliveryAfterFirstReceiveMs"] is None


def test_redelivery_does_not_mislabel_original_wait_as_current_wait(monkeypatch, records):
    monkeypatch.setattr(timing.time, "time_ns", lambda: 1_000_000_000)
    with timing.attempt(ATTRIBUTES | {"ApproximateReceiveCount": "2"}, enabled=True):
        timing.identify(job())
    record, = records
    assert record["approxFirstQueueWaitMs"] == 150
    assert record["redeliveryAfterFirstReceiveMs"] == 750
    assert record["currentReceiveQueueWaitMs"] is None


@pytest.mark.parametrize("attributes", [None, {}, {"SentTimestamp": "private@example.test"},
    ATTRIBUTES | {"SentTimestamp": "300"}, ATTRIBUTES | {"ApproximateFirstReceiveTimestamp": "1200"},
    ATTRIBUTES | {"ApproximateReceiveCount": ""}])
def test_missing_or_inconsistent_clocks_are_unmeasured(monkeypatch, records, attributes):
    monkeypatch.setattr(timing.time, "time_ns", lambda: 1_000_000_000)
    with timing.attempt(attributes, enabled=True):
        timing.identify(job())
    record, = records
    assert record["clockState"] != "ordered"
    for key in ("approxFirstQueueWaitMs", "messageAgeAtReceiveMs", "currentReceiveQueueWaitMs",
                "redeliveryAfterFirstReceiveMs"):
        assert record[key] is None
    assert "private@example.test" not in json.dumps(record)


@pytest.mark.parametrize("job_id", ["private@example.test", "\nInjected: secret", "123", JOB_ID+"\n"])
@pytest.mark.parametrize("run_id", [True, "34", -1, 2**64])
def test_arbitrary_ids_and_payload_fields_are_never_logged(records, job_id, run_id):
    with timing.attempt(ATTRIBUTES | {"private": "sensitive-body"}, enabled=True):
        timing.identify(job(job_id, {"runId": run_id, "private": "sensitive-body"}))
    record, = records
    assert record["jobId"] is record["runId"] is None
    assert record["jobIdState"] == "unsupported"
    assert "sensitive-body" not in json.dumps(record)
    assert "private@example.test" not in json.dumps(record) and "Injected" not in json.dumps(record)


def test_context_isolated_between_jobs_and_threads(records):
    @timing.database_read_timed
    def read():
        return "not logged"

    with timing.attempt({}, enabled=True):
        timing.identify(job())
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(read).result() == "not logged"
    with timing.attempt({}, enabled=True):
        timing.identify(job("11234567-89ab-4cde-8fab-0123456789ab"))
        read()
    assert records[0]["databaseRead"] is None
    assert records[1]["databaseRead"]["status"] == "success"
    assert records[0]["jobId"] != records[1]["jobId"]


class Queue:
    def __init__(self, stop, messages, failure=None):
        self.stop, self.messages, self.failure = stop, list(messages), failure
        self.calls = []
        self.published = []

    def receive_message(self, **kwargs):
        self.calls.append(("receive", kwargs))
        if not self.messages:
            self.stop.set()
            return {}
        return {"Messages": [self.messages.pop(0)]}

    def send_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        if self.failure == "publish":
            raise RuntimeError("private-publish-exception")
        self.published.append(json.loads(kwargs["MessageBody"]))

    def delete_message(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if self.failure == "delete":
            raise RuntimeError("private-delete-exception")


def run_worker(monkeypatch, *, enabled=True, failure=None, payload=None, job_id=JOB_ID):
    stop = threading.Event()
    message = {"Body": job(job_id, payload).model_dump_json(by_alias=True),
               "ReceiptHandle": "private-receipt", "Attributes": ATTRIBUTES}
    queue = Queue(stop, [message], failure)

    @timing.database_read_timed
    def read():
        if failure == "database":
            raise RuntimeError("private-database-exception")

    def execute(current):
        read()
        return ResultEnvelope(job_id=current.job_id, job_type=current.job_type, status=JobStatus.SUCCEEDED,
                              result={"private": "private-wire-result"})

    monkeypatch.setattr(worker, "execute", execute)
    settings = SimpleNamespace(jobs_queue_url="private-jobs-url", results_queue_url="private-results-url",
                               acceptance_timing_enabled=enabled)
    worker.run_loop(stop, settings, queue)
    return queue


@pytest.mark.parametrize(("failure", "outcome", "failed_stage", "deleted"), [
    (None, "success", None, True), ("database", "execution_failed", "databaseRead", False),
    ("publish", "publish_failed", "resultPublish", False), ("delete", "delete_failed", "messageDelete", True),
])
def test_worker_distinguishes_each_failure_without_changing_ack_order(monkeypatch, records,
                                                                    failure, outcome, failed_stage, deleted):
    queue = run_worker(monkeypatch, failure=failure)
    record, = records
    assert record["jobId"] == JOB_ID and record["runId"] == CALC_REQUEST["runId"]
    assert record["outcome"] == outcome
    assert queue.calls[0][1]["MessageSystemAttributeNames"] == timing.SYSTEM_ATTRIBUTES
    assert any(name == "delete" for name, _ in queue.calls) is deleted
    if failed_stage:
        assert record[failed_stage]["status"] == "failed"
    else:
        assert all(record[k]["status"] == "success" for k in ("databaseRead", "execution", "resultPublish",
                                                               "messageDelete"))
    assert "private-" not in json.dumps(record)


def test_result_failed_is_distinct_from_execution_or_publish_failure(monkeypatch, records):
    queue = run_worker(monkeypatch, payload={})
    record, = records
    assert record["outcome"] == "result_failed" and record["resultStatus"] == "FAILED"
    assert record["execution"]["status"] == record["resultPublish"]["status"] == "success"
    assert record["databaseRead"] is None
    assert [name for name, _ in queue.calls].index("send") < [name for name, _ in queue.calls].index("delete")
    assert queue.published[0]["error"]["code"] == "INVALID_INPUT"


def test_disabled_has_no_clock_logging_or_system_attribute_changes(monkeypatch, records):
    def forbidden():
        pytest.fail("비활성 계측에서 시계 또는 로그를 읽으면 안 된다")

    monkeypatch.setattr(timing.time, "time_ns", forbidden)
    monkeypatch.setattr(timing.time, "perf_counter_ns", forbidden)
    queue = run_worker(monkeypatch, enabled=False)
    assert not records
    assert queue.calls[0][1] == {"QueueUrl": "private-jobs-url", "MaxNumberOfMessages": 1, "WaitTimeSeconds": 20}
    assert queue.published[0]["status"] == "SUCCEEDED"


def test_logging_failure_does_not_change_processing_or_ack(monkeypatch, records, caplog):
    def broken(*args):
        raise RuntimeError("private-logger-exception")

    monkeypatch.setattr(timing.LOGGER, "info", broken)
    queue = run_worker(monkeypatch)
    assert queue.published[0]["status"] == "SUCCEEDED"
    assert any(name == "delete" for name, _ in queue.calls)
    assert "private-logger-exception" not in caplog.text


def test_info_handler_works_without_root_info_and_is_not_duplicated(monkeypatch, capfd):
    logger = logging.Logger("fixture.unconfigured", logging.WARNING)
    monkeypatch.setattr(timing, "LOGGER", logger)
    timing.configure_logging()
    timing.configure_logging()
    assert logger.level == logging.INFO and not logger.propagate and len(logger.handlers) == 1
    with timing.attempt({}, enabled=True):
        timing.identify(job())
    lines = capfd.readouterr().err.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event"] == "ai_calculation_timing"


def test_repository_failure_is_timed_without_exception_or_database_url(monkeypatch, records):
    monkeypatch.setattr(repository, "get_settings", lambda: SimpleNamespace(database_url=None))
    from app.errors import AppError

    with timing.attempt({}, enabled=True):
        timing.identify(job())
        with pytest.raises(AppError):
            repository.load_product_daily(3, [21], user_id=1)
    record, = records
    assert record["databaseRead"]["status"] == "failed"
    assert record["databaseRead"]["elapsedMs"] >= 0
    assert not any(key in record for key in ("userId", "warehouseId", "url", "exception", "payload"))
