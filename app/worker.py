"""SQS long-poll worker. 실패한 transient 작업은 삭제하지 않아 DLQ 정책을 따른다."""
import json
import logging
import signal

import boto3
from pydantic import ValidationError

from app.config import get_settings
from app.jobs import JobEnvelope, JobError, JobStatus, ResultEnvelope, execute

logger = logging.getLogger(__name__)
running = True


def _stop(*_: object) -> None:
    global running
    running = False


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
        return execute(job)
    except NotImplementedError:
        return _failure(job, "NOT_IMPLEMENTED", "아직 구현되지 않은 계산 기능이에요")


def run() -> None:
    settings = get_settings()
    if settings.jobs_queue_url is None or settings.results_queue_url is None:
        raise RuntimeError("IPGO_JOBS_QUEUE_URL과 IPGO_RESULTS_QUEUE_URL이 필요합니다.")
    sqs = boto3.client("sqs", region_name=settings.aws_region)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while running:
        response = sqs.receive_message(
            QueueUrl=settings.jobs_queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=900,
        )
        for message in response.get("Messages", []):
            try:
                result = process_message(message["Body"])
                sqs.send_message(
                    QueueUrl=settings.results_queue_url,
                    MessageBody=json.dumps(result.to_json_dict(), ensure_ascii=False),
                )
                sqs.delete_message(QueueUrl=settings.jobs_queue_url, ReceiptHandle=message["ReceiptHandle"])
            except ValidationError as exception:
                logger.error("invalid job envelope; leaving message for DLQ; error_type=%s",
                             exception.__class__.__name__)
            except Exception as exception:
                logger.error("engine job failed; leaving message for retry; error_type=%s",
                             exception.__class__.__name__)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
