"""HTTP와 SQS의 공통 계산 경로. 읽기 권한을 확인한 동일 DataFrame만 표본과 계산에 사용한다."""
import hashlib
import json
import math

import pandas as pd

from app.risk_policy import load_risk_policy
from engine import calculate as core
from engine.risk_sampling import prepare_risk_context
from engine.scenario import RiskInputError
from schemas.engine import AccuracyEvaluation, CalculateRequest, CalculateResponse, ScenarioRequest, ScenarioResult


def _context(request: CalculateRequest, daily: pd.DataFrame):
    policy = load_risk_policy()
    return prepare_risk_context(request, daily, policy) if policy is not None else None


def calculate(request: CalculateRequest, daily: pd.DataFrame) -> CalculateResponse:
    """업로드 snapshot의 TUNE 표본을 준비해 DA07과 기존 수량 계산에 연결한다."""
    try:
        context = _context(request, daily)
        response = core.calculate(request, daily, risk_context=context)
        # v1 digest: selected sources through asOfDate; sorted rows, explicit canonical types.
        # Stream rows so history provenance does not build a second full JSON dataset in memory.
        _, source_daily, _ = core._inputs(request, daily)
        columns = sorted(c for c in source_daily.columns if c in {
            "product_id", "upload_file_id", "stock_date", "opening_qty", "inbound_qty", "outbound_qty",
            "adjustment_qty", "closing_qty", "is_valid_observation"})
        data = source_daily.sort_values(["product_id", "stock_date", "upload_file_id"])
        digest_builder = hashlib.sha256(json.dumps(columns, separators=(",", ":")).encode() + b"\n")
        for row in data[columns].itertuples(index=False, name=None):
            values = []
            for key, value in zip(columns, row, strict=True):
                if key in ("product_id", "upload_file_id"):
                    values.append(str(int(value)))
                elif key == "stock_date":
                    values.append(value.isoformat())
                elif key == "is_valid_observation":
                    values.append(bool(value))
                else:
                    number = float(value)
                    if not math.isfinite(number):
                        raise core.CalculationInputError("이력 원본 수량이 유한하지 않아요")
                    values.append((0.0 if number == 0 else number).hex())
            digest_builder.update(json.dumps(values, separators=(",", ":"), allow_nan=False).encode() + b"\n")
        digest = digest_builder.hexdigest()
        for result in response.results:
            evaluated = context is not None and result.status.value in ("OK", "NOT_NEEDED")
            history = sorted(result.accuracy_history, key=lambda point: point.origin_date)
            result.accuracy_evaluation = AccuracyEvaluation(
                status="AVAILABLE" if history else "EMPTY" if evaluated else "UNAVAILABLE",
                reason=None if history else "NO_VALID_SAMPLES" if evaluated else "EVALUATION_UNAVAILABLE",
                period_start=context.period_start if context else None,
                period_end=context.period_end if context else None,
                observed_through=request.as_of_date, horizon_days=result.horizon_days,
                method=result.method, multiplier=request.policy.multiplier,
                policy_version=context.policy_version if context else None,
                policy_hash=context.policy_hash if context else None, data_hash=digest, history=history)
        return response
    except RiskInputError as exc:
        raise core.CalculationInputError("위험 계산 입력을 확인해 주세요") from exc


def custom_scenario(request: ScenarioRequest, daily: pd.DataFrame) -> ScenarioResult:
    """계산과 동일 정책·source·기간으로 직접 입력 시나리오 위험을 재계산한다."""
    try:
        context = _context(request.calculation_input, daily)
        return core.custom_scenario(request, daily, risk_context=context)
    except RiskInputError as exc:
        raise core.CalculationInputError("위험 계산 입력을 확인해 주세요") from exc
