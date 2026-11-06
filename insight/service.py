"""AI-01 · AI-02 인사이트 생성 (#43). prompt → LLM 1회 호출 → 응답 해석 → 출력 검사 → 응답 조립.

- FastAPI는 LLM 호출과 검사만 한다. 값 넣기 · 대조, 기본 문장 대체, 화면 mode는 Spring (#24).
- LLM이 실패해도 예외가 아니라 FAILED 응답이다. Spring이 기본 문장으로 대체한다.
- 로그에는 사유 코드 · 응답시간 · 비용만 남기고 prompt와 생성 문장 원문은 남기지 않는다 (AI-03).
"""
import logging

from insight.llm import LLMClient, LLMError, LLMResult
from insight.prompt import PROMPT_VERSION, build_prompt, draft_text
from insight.validator import check_draft
from schemas.insight import FailReason, InsightInput, InsightResponse, Validation

logger = logging.getLogger(__name__)


def _failed(reason: FailReason, latency_ms: int, result: LLMResult | None = None) -> InsightResponse:
    # Only a received provider result can supply actual model and usage.
    return InsightResponse(validation=Validation.FAILED, draft=None, fail_reason=reason,
                           latency_ms=latency_ms, prompt_version=PROMPT_VERSION, **_metadata(result))


def _metadata(result: LLMResult | None) -> dict:
    if result is None:
        return {"model_name": None, "input_tokens": None, "output_tokens": None, "cost_usd": None}
    return {"model_name": result.model, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "cost_usd": result.cost_usd}


def generate_insight(inp: InsightInput, client: LLMClient) -> InsightResponse:
    """InsightInput 하나로 자리 표시자 문장 초안을 만들고 검사한다. 입력이 잘못되면 InsightInputError."""
    prompt = build_prompt(inp)
    try:
        r = client.complete(prompt.system, prompt.user)
    except LLMError as e:
        logger.info("insight failed reason=%s latency_ms=%d prompt=%s", e.reason.value, e.latency_ms, PROMPT_VERSION)
        return _failed(e.reason, e.latency_ms, None)

    text = None if r.truncated else draft_text(r.text)
    detail = "truncated" if r.truncated else "format"
    if text is not None:
        result = check_draft(text, prompt.allowed, prompt.required)
        detail = None if result.ok else result.violations[0].code
    logger.info("insight %s detail=%s model=%s latency_ms=%d tokens=%s/%s cost_usd=%s prompt=%s",
                "passed" if detail is None else "failed", detail, r.model, r.latency_ms,
                r.input_tokens, r.output_tokens, r.cost_usd, PROMPT_VERSION)
    if detail is not None:
        return _failed(FailReason.VALIDATION_FAILED, r.latency_ms, r)
    return InsightResponse(validation=Validation.PASSED, draft=text, fail_reason=None,
                           latency_ms=r.latency_ms, prompt_version=PROMPT_VERSION, **_metadata(r))
