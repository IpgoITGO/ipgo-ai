"""SQS job/result envelope와 engine dispatch 계약."""
from enum import StrEnum

from pydantic import Field, ValidationError

from app import calculation, repository, storage
from app.execution import engine_slot, validate_calculation_capacity
from engine import clean as cleaning
from engine.backtest import run_backtest
from schemas.base import CamelModel
from schemas.engine import BacktestRequest, CalculateRequest, CleanRequest


class JobType(StrEnum):
    CLEAN = "CLEAN"
    CALCULATE = "CALCULATE"
    BACKTEST = "BACKTEST"


class JobStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class JobEnvelope(CamelModel):
    contract_version: str = Field(pattern=r"^1$")
    job_id: str = Field(min_length=1, max_length=100)
    job_type: JobType
    payload: dict


class JobError(CamelModel):
    code: str
    message: str


class ResultEnvelope(CamelModel):
    contract_version: str = "1"
    job_id: str
    job_type: JobType
    status: JobStatus
    result: dict | None = None
    error: JobError | None = None


def execute(job: JobEnvelope) -> ResultEnvelope:
    with engine_slot():
        return _execute(job)


def _execute(job: JobEnvelope) -> ResultEnvelope:
    if job.job_type is JobType.CLEAN:
        try:
            request = CleanRequest.model_validate(job.payload)
        except ValidationError as exc:
            raise cleaning.CleaningInputError("정제 요청 형식이 올바르지 않아요") from exc
        response = clean_request(request)
    elif job.job_type is JobType.CALCULATE:
        request = CalculateRequest.model_validate(job.payload)
        validate_calculation_capacity(request)
        daily = repository.load_product_daily(request.warehouse_id, request.upload_file_ids, user_id=request.user_id)
        response = calculation.calculate(request, daily)
    else:
        request = BacktestRequest.model_validate(job.payload)
        daily = repository.load_product_daily(request.warehouse_id, request.upload_file_ids, user_id=request.user_id)
        response = run_backtest(request, daily)
    return ResultEnvelope(
        job_id=job.job_id,
        job_type=job.job_type,
        status=JobStatus.SUCCEEDED,
        result=response.to_json_dict(),
    )


def clean_request(request: CleanRequest):
    """HTTP와 worker가 공유하는 정제 조립 경로."""
    source = request.files[0]
    if request.upload_id != source.upload_file_id:
        raise cleaning.CleaningInputError("업로드 식별자가 일치하지 않아요")
    known, previous, period = repository.load_cleaning_context(
        request.user_id, request.warehouse_id, request.upload_id, request.previous_upload_file_id, source)
    content = (repository.load_upload_original(request.user_id, request.warehouse_id, source)
               if source.storage_key and source.storage_key.startswith("db/") else storage.read_source_file(source))
    return cleaning.clean([(source, content)], known, previous, period)
