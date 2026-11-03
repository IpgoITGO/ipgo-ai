"""HTTP와 SQS의 공통 계산 경로. 읽기 권한을 확인한 동일 DataFrame만 표본과 계산에 사용한다."""
import pandas as pd

from app.risk_policy import load_risk_policy
from engine import calculate as core
from engine.risk_sampling import prepare_risk_context
from engine.scenario import RiskInputError
from schemas.engine import CalculateRequest, CalculateResponse, ScenarioRequest, ScenarioResult


def _context(request: CalculateRequest, daily: pd.DataFrame):
    policy = load_risk_policy()
    return prepare_risk_context(request, daily, policy) if policy is not None else None


def calculate(request: CalculateRequest, daily: pd.DataFrame) -> CalculateResponse:
    """업로드 snapshot의 TUNE 표본을 준비해 DA07과 기존 수량 계산에 연결한다."""
    try:
        context = _context(request, daily)
        return core.calculate(request, daily, risk_context=context)
    except RiskInputError as exc:
        raise core.CalculationInputError("위험 계산 입력을 확인해 주세요") from exc


def custom_scenario(request: ScenarioRequest, daily: pd.DataFrame) -> ScenarioResult:
    """계산과 동일 정책·source·기간으로 직접 입력 시나리오 위험을 재계산한다."""
    try:
        context = _context(request.calculation_input, daily)
        return core.custom_scenario(request, daily, risk_context=context)
    except RiskInputError as exc:
        raise core.CalculationInputError("위험 계산 입력을 확인해 주세요") from exc
