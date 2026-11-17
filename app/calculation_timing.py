"""CALCULATE 시도 한 건의 허용된 상관키·경과 시간만 기록한다(#121)."""
import json
import logging
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

LOGGER = logging.getLogger("ipgo.calculation_timing")
_CURRENT = ContextVar("calculation_timing", default=None)
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
SYSTEM_ATTRIBUTES = ["SentTimestamp", "ApproximateFirstReceiveTimestamp", "ApproximateReceiveCount"]


def configure_logging():
    """uvicorn의 root 레벨과 무관하게 이 JSON 로그만 INFO로 출력한다."""
    try:
        if not LOGGER.handlers:
            handler = logging.StreamHandler(sys.__stderr__)
            handler.setFormatter(logging.Formatter("%(message)s"))
            LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        LOGGER.propagate = False
    except Exception:
        # 계측 출력의 실패로 큐 처리/재전달 정책을 바꾸지 않는다.
        pass


def _positive_int(value):
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def _attribute(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 16:
        return _positive_int(int(value))
    return None


def _elapsed(start):
    elapsed = time.perf_counter_ns() - start
    return elapsed / 1_000_000 if elapsed >= 0 else None


class CalculationTiming:
    def __init__(self, attributes, *, enabled=True):
        self.enabled = enabled
        self.attributes = attributes if isinstance(attributes, dict) else {}
        self.received_at = time.time_ns() // 1_000_000 if enabled else None
        self.started = time.perf_counter_ns() if enabled else None
        self.active = False
        self.job_id = self.run_id = None
        self.job_id_state = "unsupported"
        self.outcome = "execution_failed"
        self.result_status = None
        self.stages = {}

    def identify(self, job):
        if self.enabled and job.job_type.value == "CALCULATE":
            self.active = True
            if _UUID.fullmatch(job.job_id):
                self.job_id, self.job_id_state = job.job_id.lower(), "uuid"
            self.run_id = _positive_int(job.payload.get("runId"))

    @contextmanager
    def phase(self, name):
        if not self.enabled:
            yield
            return
        start = time.perf_counter_ns()
        status = "failed"
        try:
            yield
            status = "success"
        finally:
            self.stages[name] = {"elapsedMs": _elapsed(start),
                                 "status": status}

    def record(self):
        sent = _attribute(self.attributes.get("SentTimestamp"))
        first = _attribute(self.attributes.get("ApproximateFirstReceiveTimestamp"))
        count = _attribute(self.attributes.get("ApproximateReceiveCount"))
        valid = sent is not None and first is not None and count is not None
        consistent = valid and sent <= first <= self.received_at
        clock_state = "ordered" if consistent else "inconsistent" if valid else "missing_or_invalid"
        return {
            "event": "ai_calculation_timing", "schemaVersion": 1,
            "jobId": self.job_id, "jobIdState": self.job_id_state, "runId": self.run_id,
            "outcome": self.outcome, "resultStatus": self.result_status,
            "sentTimestampMs": sent, "approxFirstReceiveTimestampMs": first,
            "receivedAtMs": self.received_at, "approxReceiveCount": count,
            "clockState": clock_state,
            "approxFirstQueueWaitMs": first - sent if consistent else None,
            "messageAgeAtReceiveMs": self.received_at - sent if consistent else None,
            "currentReceiveQueueWaitMs": first - sent if consistent and count == 1 else None,
            "redeliveryAfterFirstReceiveMs": self.received_at - first if consistent and count > 1 else None,
            "attemptElapsedMs": _elapsed(self.started),
            "databaseRead": self.stages.get("databaseRead"),
            "execution": self.stages.get("execution"),
            "resultPublish": self.stages.get("resultPublish"),
            "messageDelete": self.stages.get("messageDelete"),
        }

    def emit(self):
        if self.active:
            try:
                LOGGER.info(json.dumps(self.record(), ensure_ascii=False, allow_nan=False))
            except Exception:
                pass


@contextmanager
def attempt(attributes, *, enabled=False):
    timing = CalculationTiming(attributes, enabled=enabled)
    if not enabled:
        yield timing
        return
    token = _CURRENT.set(timing)
    try:
        yield timing
    finally:
        _CURRENT.reset(token)
        timing.emit()


def identify(job):
    timing = _CURRENT.get()
    if timing is not None:
        timing.identify(job)


def database_read_timed(function):
    @wraps(function)
    def measured(*args, **kwargs):
        timing = _CURRENT.get()
        if timing is None or not timing.active:
            return function(*args, **kwargs)
        with timing.phase("databaseRead"):
            return function(*args, **kwargs)
    return measured
