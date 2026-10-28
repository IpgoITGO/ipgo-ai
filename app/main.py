"""ipgo-ai FastAPI 진입점.

이 파일은 라우터를 연결만 한다. 엔드포인트는 각자 routers/ 파일에 쓴다.
    routers/engine.py  — 장준영 (/engine/clean · /calculate · /scenarios · /backtest)
    routers/insight.py — 이석형 (/engine/insight)
"""
from fastapi import FastAPI

from app.config import get_settings
from app.errors import register_error_handlers
from app.routers import engine as engine_router
from app.routers import insight as insight_router
from engine import ENGINE_VERSION

app = FastAPI(title="ipgo-ai", version=ENGINE_VERSION)
register_error_handlers(app)

app.include_router(engine_router.router, prefix="/engine", tags=["engine"])
app.include_router(insight_router.router, prefix="/engine", tags=["insight"])


@app.get("/health", tags=["ops"])
def health() -> dict:
    """배포 상태 확인용."""
    return {"status": "ok", "engineVersion": ENGINE_VERSION, "mode": get_settings().engine_mode}
