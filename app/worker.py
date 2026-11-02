"""SQS worker. Result queue must allow at least 1 MiB messages.

Jobs over 120 seconds need visibility renewal; the current 900-second visibility
is not renewed. Jobs exceeding it may run twice;
result consumers must deduplicate by job ID. Shutdown has a bounded join, so an
in-flight job can also be redelivered after termination.
"""
import json
import logging
import signal
import threading

import boto3
from botocore.config import Config
from pydantic import ValidationError

from app.config import get_settings
from app.jobs import JobEnvelope, JobError, JobStatus, JobType, ResultEnvelope, execute
from engine.calculate import CalculationInputError
from schemas.engine import BacktestRequest, CalculateRequest, CleanRequest

logger = logging.getLogger(__name__)
MAX_RESULT_BYTES = 900_000


def _failure(job: JobEnvelope, code: str, message: str) -> ResultEnvelope:
    return ResultEnvelope(
        job_id=job.job_id,
        job_type=job.job_type,
        status=JobStatus.FAILED,
        error=JobError(code=code, message=message),
    )


def process_message(body: str) -> ResultEnvelope:
    job = JobEnvelope.model_validate_json(body)
    try:
        {JobType.CLEAN: CleanRequest, JobType.CALCULATE: CalculateRequest,
         JobType.BACKTEST: BacktestRequest}[job.job_type].model_validate(job.payload)
    except ValidationError:
        return _failure(job, "INVALID_INPUT", "입력값을 확인해 주세요.")
    try:
        return execute(job)
    except CalculationInputError:
        return _failure(job, "INVALID_INPUT", "입력값을 확인해 주세요.")
    except NotImplementedError:
        return _failure(job, "NOT_IMPLEMENTED", "아직 구현되지 않은 계산 기능이에요.")


def create_worker():
    settings = get_settings()
    if not settings.jobs_queue_url or not settings.results_queue_url:
        raise RuntimeError("IPGO_JOBS_QUEUE_URL과 IPGO_RESULTS_QUEUE_URL이 필요합니다.")
    sqs = boto3.client(
        "sqs", region_name=settings.aws_region,
        config=Config(connect_timeout=3, read_timeout=25, retries={"total_max_attempts": 2, "mode": "standard"}),
    )
    return settings, sqs


def run_loop(stop: threading.Event, settings, sqs) -> None:
    while not stop.is_set():
        response = sqs.receive_message(
            QueueUrl=settings.jobs_queue_url, MaxNumberOfMessages=1,
            WaitTimeSeconds=20, VisibilityTimeout=900,
        )
        for message in response.get("Messages", []):
            if stop.is_set():
                break
            try:
                result = process_message(message["Body"])
                body = json.dumps(result.to_json_dict(), ensure_ascii=False)
                if len(body.encode("utf-8")) > MAX_RESULT_BYTES:
                    job = JobEnvelope.model_validate_json(message["Body"])
                    result = _failure(job, "RESULT_TOO_LARGE", "결과가 너무 커서 처리할 수 없어요.")
                    body = json.dumps(result.to_json_dict(), ensure_ascii=False)
                sqs.send_message(QueueUrl=settings.results_queue_url, MessageBody=body)
                sqs.delete_message(QueueUrl=settings.jobs_queue_url, ReceiptHandle=message["ReceiptHandle"])
            except ValidationError:
                logger.error("invalid job envelope; leaving message for DLQ")
            except Exception:
                logger.error("engine job failed; leaving message for retry")


def run() -> None:
    settings, sqs = create_worker()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run_loop(stop, settings, sqs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
