"""AI 인사이트 전송 계약 (#10, 0002, #24 · Server #25).

FastAPI는 이름 목록으로 초안 생성·검사만 한다. 실제 값 삽입·대조·기본문장·mode는 Spring 책임이다.
계산 결과→입력 변환 구현은 #36의 별도 작업이며 이 스키마에 포함하지 않는다.
"""
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

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
    directions: dict[str, Literal["높음", "낮음", "같음"]]
    risk_status: RiskStatus
    # 기존 이름 목록 요청도 수용한다. 새 변환기는 아래 계산 메타데이터를 명시적으로 채운다 (#36).
    scenarios: list[ScenarioType] = Field(default_factory=list)
    method_fallback: bool = False
    start_stock_estimated: bool = False
    has_no_date_receipts: bool = False

    @model_validator(mode="after")
    def _scenario_names_match_quantities(self) -> InsightInput:
        if self.scenarios:
            present = {scenario for scenario in ScenarioType if f"qty_{scenario.value.lower()}" in self.placeholders}
            if len(set(self.scenarios)) != len(self.scenarios) or set(self.scenarios) != present:
                raise ValueError("scenarios는 qty 자리 표시자가 있는 시나리오를 중복 없이 담아야 해요")
        return self


class InsightRequest(CamelModel):
    result_id: int
    input: InsightInput


class InsightResponse(CamelModel):
    validation: Validation
    draft: str | None                       # 자리 표시자가 들어간 문장 초안 (통과했을 때)
    fail_reason: FailReason | None = None
    latency_ms: int
    cost_usd: float
    prompt_version: str = Field(min_length=1)  # 실제 서비스의 PROMPT_VERSION, 모의 응답은 "mock"
