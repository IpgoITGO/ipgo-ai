"""계산 엔진 엔드포인트 (담당: 장준영). API 명세서 4장."""
from fastapi import APIRouter

from app import repository, storage
from app.config import get_settings
from app.mocks import load_mock
from engine import calculate as calc
from engine import clean as cleaning
from engine.backtest import run_backtest
from schemas.engine import (
    BacktestRequest,
    BacktestResponse,
    CalculateRequest,
    CalculateResponse,
    CleanRequest,
    CleanResponse,
    ScenarioRequest,
    ScenarioResult,
)

router = APIRouter()


def _mock_mode() -> bool:
    return get_settings().engine_mode == "mock"


@router.post("/clean", response_model=CleanResponse, response_model_by_alias=True)
def clean(req: CleanRequest) -> CleanResponse:
    """DA-01 · DA-02 정제·검증."""
    if _mock_mode():
        return load_mock("clean", CleanResponse)
    files = [(f, storage.read_source_file(f)) for f in req.files]
    return cleaning.clean(files)


@router.post("/calculate", response_model=CalculateResponse, response_model_by_alias=True)
def calculate(req: CalculateRequest) -> CalculateResponse:
    """DA-03 ~ DA-07 · DA-11 · DA-13 입고 계산."""
    if _mock_mode():
        return load_mock("calculate", CalculateResponse)
    daily = repository.load_product_daily(req.warehouse_id, req.upload_file_ids)
    return calc.calculate(req, daily)


@router.post("/scenarios", response_model=ScenarioResult, response_model_by_alias=True)
def scenarios(req: ScenarioRequest) -> ScenarioResult:
    """DA-06 직접 입력 수량 시나리오."""
    if _mock_mode():
        return load_mock("scenarios", ScenarioResult)
    ci = req.calculation_input
    daily = repository.load_product_daily(ci.warehouse_id, ci.upload_file_ids)
    return calc.custom_scenario(req, daily)


@router.post("/backtest", response_model=BacktestResponse, response_model_by_alias=True)
def backtest(req: BacktestRequest) -> BacktestResponse:
    """DA-08 ~ DA-10 · DA-12 · DA-13 정확도 평가 (팀 내부용)."""
    if _mock_mode():
        return load_mock("backtest", BacktestResponse)
    daily = repository.load_product_daily(req.warehouse_id, req.upload_file_ids)
    return run_backtest(req, daily)
