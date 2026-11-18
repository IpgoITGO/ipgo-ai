"""SQS worker가 계산 입력 오류의 실제 이유를 결과 메시지에 담는지 확인한다(#143)."""
import json
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from app import jobs, worker
from app.jobs import JobStatus, JobType
from engine.calculate import CalculationInputError
from tests.payloads import CALC_REQUEST


def _body(payload=None, job_id="job-143"):
    return json.dumps({"contractVersion": "1", "jobId": job_id, "jobType": JobType.CALCULATE,
                       "payload": payload if payload is not None else CALC_REQUEST})


def _raise(exc):
    def run(*_args, **_kwargs):
        raise exc
    return run


class Queue:
    """결과 queue에 보낸 메시지를 모은다. 메시지 1개를 처리한 다음 수신에서 worker를 멈춘다."""

    def __init__(self, body, stop):
        self.body, self.stop, self.published, self.deleted, self.received = body, stop, [], False, 0

    def receive_message(self, **_kwargs):
        self.received += 1
        if self.received > 1:
            self.stop.set()
            return {}
        return {"Messages": [{"Body": self.body, "ReceiptHandle": "handle"}]}

    def send_message(self, **kwargs):
        self.published.append(json.loads(kwargs["MessageBody"]))

    def delete_message(self, **_kwargs):
        self.deleted = True


def _run_worker(body):
    stop = threading.Event()
    queue = Queue(body, stop)
    settings = SimpleNamespace(jobs_queue_url="jobs-url", results_queue_url="results-url")
    worker.run_loop(stop, settings, queue)
    return queue


def test_real_capacity_error_reaches_result_message_with_its_sentence():
    # 실제 계산 경로: 기준일 10/14, 오늘 2028-10-14 → 간격 731일 > 730일. DB를 읽기 전에 막힌다.
    payload = {**CALC_REQUEST, "today": "2028-10-14"}
    queue = _run_worker(_body(payload))
    message, = queue.published
    assert message["status"] == "FAILED"
    assert message["error"]["code"] == "INVALID_INPUT"
    assert message["error"]["message"] == "기준일과 오늘 사이 간격은 최대 730일을 지원해요"
    assert queue.deleted


@pytest.mark.parametrize(("raised", "expected"), [
    # 11/18 QA: 리드타임 23일 + 입고 주기 555일 = 578일 > 366일. 실제 이유가 셀러에게 가야 한다.
    ("상품별 계산 기간은 최대 366일을 지원해요", "상품별 계산 기간은 최대 366일을 지원해요"),
    # 이유가 비어 있으면 지금까지의 기본 문장을 쓴다.
    ("", "입력값을 확인해 주세요."),
])
def test_calculation_input_error_sends_real_sentence_or_default(monkeypatch, raised, expected):
    # #141이 366일 검사 위치를 바꿔도 이 계약은 유지돼야 하므로 오류 문장만 고정해 던진다.
    monkeypatch.setattr(jobs, "validate_calculation_capacity", _raise(CalculationInputError(raised)))
    result = worker.process_message(_body())
    assert result.status is JobStatus.FAILED
    assert (result.error.code, result.error.message) == ("INVALID_INPUT", expected)
    assert result.error.details is None


def test_request_format_error_keeps_general_sentence():
    payload = {**CALC_REQUEST, "settings": [{**CALC_REQUEST["settings"][0], "leadTimeDays": "private-value"}]}
    result = worker.process_message(_body(payload))
    assert (result.error.code, result.error.message) == ("INVALID_INPUT", "입력값을 확인해 주세요.")
    assert "private-value" not in result.model_dump_json()


def test_engine_failure_publishes_nothing_internal(monkeypatch):
    # 엔진 장애·알 수 없는 오류는 결과 메시지를 만들지 않고 재시도(DLQ)로 남긴다. 내부 문장은 나가지 않는다.
    monkeypatch.setattr(jobs.repository, "load_product_daily", lambda *_a, **_k: pd.DataFrame())
    monkeypatch.setattr(jobs.calculation, "calculate", _raise(RuntimeError("private /srv/db.internal stack")))
    with pytest.raises(RuntimeError):
        worker.process_message(_body())
    queue = _run_worker(_body())
    assert queue.received == 2 and queue.published == [] and not queue.deleted
