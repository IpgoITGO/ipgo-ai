"""AI 인사이트 입출력 계약 — 초안 v0.1 (담당: 이석형 · 이 파일은 확인용 초안)

기준: API 명세서 4장 /engine/insight, 5장.
계산 엔진(engine.ProductResult)에서 이 입력을 만드는 변환 함수는
엔진 쪽(engine/insight_facts.py, 장준영)에 두는 것을 제안한다 → DA-11 "AI 작성 문장에 쓸 구조화 결과".
"""
from enum import StrEnum

from .base import CamelModel, RiskStatus, ScenarioType


class Validation(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class FailReason(StrEnum):
    LLM_ERROR = "LLM_ERROR"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    # VALUE_MISMATCH는 Spring에서만 발생


class InsightInput(CamelModel):
    """LLM 프롬프트에 들어가는 구조화 결과. 원본 수불부·원가·계정 정보·다른 SKU는 넣지 않는다 (AI-03)."""
    sku_label: str                          # "상품 A" (실제 상품명 대신)
    recommended: ScenarioType               # 보통 P100
    placeholders: list[str]                 # ["qty_p80", "qty_p100", ..., "risk_pre"]
    directions: dict[str, str]              # {"risk_post_p80_vs_p100": "높음", ...}
    risk_status: RiskStatus


class InsightRequest(CamelModel):
    result_id: int
    input: InsightInput


class InsightResponse(CamelModel):
    validation: Validation
    draft: str | None                       # 자리 표시자가 들어간 문장 초안 (통과했을 때)
    fail_reason: FailReason | None = None
    latency_ms: int
    cost_usd: float
