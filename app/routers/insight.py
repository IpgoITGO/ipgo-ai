"""AI 인사이트 엔드포인트 (담당: 이석형). API 명세서 4장 · 5장, #43.

FastAPI는 LLM 호출과 출력 검사만 하고, 값 넣기 · 대조 · 기본 문장 대체는 Spring이 한다 (#24).
"""
import os
from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.errors import AppError
from app.mocks import load_mock
from insight.llm import LLMClient, OpenAIClient
from insight.prompt import InsightInputError
from insight.service import generate_insight
from schemas.insight import InsightRequest, InsightResponse

router = APIRouter()


def _mock_mode() -> bool:
    return get_settings().engine_mode == "mock"


def get_llm_client() -> LLMClient | None:
    """real 모드에서만 OpenAI 호출기를 만든다. mock 배포에는 OPENAI_API_KEY가 없다 (#39). 테스트는 바꿔 끼운다."""
    if _mock_mode():
        return None
    if not os.getenv("OPENAI_API_KEY"):
        raise AppError(501, "LLM_NOT_CONFIGURED", "AI 인사이트 LLM 키가 설정되지 않았어요")
    return OpenAIClient()


@router.post("/insight", response_model=InsightResponse, response_model_by_alias=True)
def insight(req: InsightRequest, client: Annotated[LLMClient | None, Depends(get_llm_client)]) -> InsightResponse:
    """AI-01 ~ AI-03 자리 표시자 문장 초안 생성 · 검사. LLM 실패도 200 + FAILED (Spring이 기본 문장으로 대체)."""
    if client is None:
        return load_mock("insight", InsightResponse)
    try:
        return generate_insight(req.input, client)
    except InsightInputError as e:
        raise AppError(422, "INVALID_INPUT", str(e)) from e
