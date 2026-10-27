"""오류 응답을 명세 1장 형식 {"code", "message", "details"}로 통일한다."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from schemas.base import ErrorResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """의도한 오류. 상태 코드와 code를 직접 정한다."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _json(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    body = ErrorResponse(code=code, message=message, details=details).to_json_dict()
    return JSONResponse(status_code=status, content=body)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _json(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errors = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "type": e["type"], "message": e["msg"]}
            for e in exc.errors()
        ]
        return _json(422, "INVALID_INPUT", "요청 형식이 연동 명세와 맞지 않아요", {"errors": errors})

    @app.exception_handler(NotImplementedError)
    async def _not_implemented(_: Request, exc: NotImplementedError):
        return _json(501, "NOT_IMPLEMENTED", "아직 구현되지 않은 기능이에요", {"feature": str(exc)})

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        logger.exception("unexpected engine error")
        return _json(500, "ENGINE_ERROR", "계산 엔진에서 오류가 났어요")  # 내부 내용은 응답에 넣지 않음
