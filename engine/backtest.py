"""DA-08 반복 백테스트 · DA-09 정책 선정 · DA-10 시나리오 신뢰도 · DA-12 실제 입고 비교.

가장 중요한 규칙: 기준일 t의 예측에는 t일까지의 정보만 쓴다.
오차(MAE · WAPE)는 올림 전 소수 예측값으로 계산한다.
"""
import pandas as pd

from schemas.engine import BacktestRequest, BacktestResponse


def run_backtest(request: BacktestRequest, product_daily: pd.DataFrame) -> BacktestResponse:
    raise NotImplementedError("DA-08 run_backtest")
