"""DA-11 계산 실행: 상품마다 예측 → 수량 → 시나리오 → 위험 → 등급을 묶어 결과를 만든다."""
import pandas as pd

from schemas.engine import CalculateRequest, CalculateResponse, ScenarioRequest, ScenarioResult


def calculate(request: CalculateRequest, product_daily: pd.DataFrame) -> CalculateResponse:
    raise NotImplementedError("DA-11 calculate")


def custom_scenario(request: ScenarioRequest, product_daily: pd.DataFrame) -> ScenarioResult:
    raise NotImplementedError("DA-06 custom_scenario")
