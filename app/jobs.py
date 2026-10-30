"""SQS job/result envelope와 engine dispatch 계약."""
from enum import StrEnum

from pydantic import Field

from app import repository, storage
from engine import calculate as calculation
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
    if job.job_type is JobType.CLEAN:
        request = CleanRequest.model_validate(job.payload)
        sources = [(source, storage.read_source_file(source)) for source in request.files]
        response = cleaning.clean(sources, repository.load_known_barcodes(request.user_id))
    elif job.job_type is JobType.CALCULATE:
        request = CalculateRequest.model_validate(job.payload)
        daily = repository.load_product_daily(request.warehouse_id, request.upload_file_ids)
        response = calculation.calculate(request, daily)
    else:
        request = BacktestRequest.model_validate(job.payload)
        daily = repository.load_product_daily(request.warehouse_id, request.upload_file_ids)
        response = run_backtest(request, daily)
    return ResultEnvelope(
        job_id=job.job_id,
        job_type=job.job_type,
        status=JobStatus.SUCCEEDED,
        result=response.to_json_dict(),
    )
