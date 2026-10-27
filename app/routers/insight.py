"""AI 인사이트 엔드포인트 (담당: 이석형). API 명세서 4장 · 5장.

뼈대용 임시 코드다. 이석형 님의 기존 api.py 로직으로 바꾼다.
"""
from fastapi import APIRouter

from app.config import get_settings
from app.mocks import load_mock
from schemas.insight import InsightRequest, InsightResponse

router = APIRouter()


@router.post("/insight", response_model=InsightResponse, response_model_by_alias=True)
def insight(req: InsightRequest) -> InsightResponse:
    if get_settings().engine_mode == "mock":
        return load_mock("insight", InsightResponse)
    raise NotImplementedError("AI-01 insight")
