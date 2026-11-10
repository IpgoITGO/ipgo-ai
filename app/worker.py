"""SQS worker. Result queue must allow at least 1 MiB messages.

Jobs over 120 seconds need visibility renewal; the current 900-second visibility
is not renewed. Jobs exceeding it may run twice;
result consumers must deduplicate by job ID. Shutdown has a bounded join, so an
in-flight job can also be redelivered after termination.
"""
import logging
import signal
import threading

import boto3
from botocore.config import Config
from pydantic import ValidationError

from app.config import get_settings
from app.jobs import JobEnvelope, JobError, JobErrorDetails, JobStatus, JobType, ResultEnvelope, execute
from app.result_transport import MAX_RESULT_BYTES as MAX_RESULT_BYTES
from app.result_transport import encode_result
from engine.calculate import CalculationInputError
from engine.clean import CleaningInputError
from schemas.engine import BacktestRequest, CalculateRequest, CleanRequest

logger = logging.getLogger(__name__)


def _failure(job: JobEnvelope, code: str, message: str, reason: str | None = None) -> ResultEnvelope:
    details = JobErrorDetails(reason=reason) if reason else None
    return ResultEnvelope(
        job_id=job.job_id,
        job_type=job.job_type,
        status=JobStatus.FAILED,
        error=JobError(code=code, message=message, details=details),
    )


def process_message(body: str) -> ResultEnvelope:
    job = JobEnvelope.model_validate_json(body)
    try:
        {JobType.CLEAN: CleanRequest, JobType.CALCULATE: CalculateRequest,
         JobType.BACKTEST: BacktestRequest}[job.job_type].model_validate(job.payload)
    except ValidationError:
        code = "INVALID_SOURCE" if job.job_type is JobType.CLEAN else "INVALID_INPUT"
        return _failure(job, code, "입력값을 확인해 주세요.")
    try:
        return execute(job)
    except CleaningInputError as exc:
        # 정제 오류 문장은 셀러가 고칠 내용을 담고 있어 그대로 보낸다(#94). HTTP 경로와 같은 문장이다.
        return _failure(job, "INVALID_SOURCE", str(exc) or "원본을 확인해 주세요.", exc.reason)
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
        try:
            response = sqs.receive_message(
                QueueUrl=settings.jobs_queue_url, MaxNumberOfMessages=1,
                WaitTimeSeconds=20, VisibilityTimeout=900,
            )
        except Exception:
            logger.warning("queue receive failed; retrying")
            stop.wait(2)
            continue
        for message in response.get("Messages", []):
            if stop.is_set():
                break
            try:
                result = process_message(message["Body"])
                try:
                    body = encode_result(result)
                except ValueError:
                    job = JobEnvelope.model_validate_json(message["Body"])
                    result = _failure(job, "RESULT_TOO_LARGE", "결과가 너무 커서 처리할 수 없어요.")
                    body = encode_result(result)
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
