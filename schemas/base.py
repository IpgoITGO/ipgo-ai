"""공용 기반 모델과 상태값.

- API 필드는 camelCase, 파이썬 코드는 snake_case (연동 명세 1장).
- 날짜는 date(YYYY-MM-DD), 위험·비율은 0~1 소수.
- 수량은 엔진에서 소수(float). 입고 단위로 올린 수량과 금액만 정수 (docs/decisions/0001).
"""
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """JSON은 camelCase로 주고받고, 코드에서는 snake_case로 쓴다."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,   # 코드에서 snake_case 이름으로도 만들 수 있게
        extra="forbid",          # 명세에 없는 필드가 오면 바로 오류 → 계약 어긋남 조기 발견
    )

    def to_json_dict(self) -> dict:
        """Spring에 돌려줄 때 쓰는 camelCase dict."""
        return self.model_dump(by_alias=True, mode="json")


class ErrorResponse(CamelModel):
    """오류 응답. 명세 1장과 같은 형식이라 Spring이 ENGINE_ERROR 등으로 바꾸기 쉽다."""

    code: str
    message: str
    details: dict | None = None


# ---- 상태값 (연동 명세 6장 "정상 응답 안의 상태값") ----

class ResultStatus(StrEnum):
    OK = "OK"
    NOT_NEEDED = "NOT_NEEDED"
    NOT_CALCULABLE = "NOT_CALCULABLE"
    EXCLUDED = "EXCLUDED"
    REUPLOAD_REQUIRED = "REUPLOAD_REQUIRED"


class Method(StrEnum):
    A = "A"  # 작년 동일 커버 기간 × 직전 90일 추세
    B = "B"  # 최근 유효 관측일 평균 × 커버 기간


class RiskStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    LOW_GRADE = "LOW_GRADE"


class AccuracyGrade(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ScenarioType(StrEnum):
    P80 = "P80"
    P100 = "P100"
    P120 = "P120"
    CUSTOM = "CUSTOM"
