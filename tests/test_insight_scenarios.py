"""실패 · 기본 문장 대체 시나리오의 FastAPI 쪽 기준 (#44). 테스트 이름의 sN은 #44 시나리오 표 번호다.

FastAPI가 관여하는 시나리오만 여기서 본다. Spring 쪽(5 · 6 · 9~14)의 기대 문장은 test_insight_fallback.py와
tests/fixtures/insight_fallback.json, 화면 쪽은 IpgoITGO/ipgo-client#80에서 본다. 가짜 호출기로만 돈다.

실패를 일부러 만드는 방법 (로컬 real 모드, Spring 연동 시험용):
- s2 시간 초과: LLM_TIMEOUT_SEC=0.01
- s3 호출 오류: OPENAI_MODEL=없는-모델
- s4 검사 탈락: placeholders에서 예시 키를 빼 LLM이 목록에 없는 키를 쓰게 함 (#43 실제 호출에서 확인)
- s6 무응답: FastAPI를 끄거나 Spring 쪽 시간 제한(15초)보다 오래 걸리게 함
- s7 키 없음: ENGINE_MODE=real, OPENAI_API_KEY 없이 실행
- s8 잘못된 입력: placeholders에 qty_p100이 없는 요청
"""
import json

import pytest

from app.main import app
from app.routers.insight import get_llm_client
from insight.llm import FakeClient, LLMError, LLMTimeout
from tests.payloads import INSIGHT_REQUEST

DRAFT = json.dumps({"sentences": [
    "권장 입고량 {{qty_p100}}개를 넣으면 도착 후 품절 위험은 {{risk_basis}} 추정치로 {{risk_post_p100}}입니다.",
    "새 물량이 도착하기 전까지의 품절 위험은 수량과 관계없이 {{risk_pre}}입니다."]}, ensure_ascii=False)


@pytest.fixture
def real_client(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # 호출기는 바꿔 끼우므로 실제로 쓰지 않음
    yield client
    app.dependency_overrides.clear()


def _post(client, llm, body=INSIGHT_REQUEST):
    app.dependency_overrides[get_llm_client] = lambda: llm
    return client.post("/engine/insight", json=body)


def test_s1_passed_returns_placeholder_draft(real_client):
    res = _post(real_client, FakeClient([DRAFT]))
    assert res.status_code == 200
    body = res.json()
    assert (body["validation"], body["failReason"]) == ("PASSED", None)
    assert "{{qty_p100}}" in body["draft"]  # 값은 Spring이 넣는다


@pytest.mark.parametrize(
    ("llm", "reason"),
    [
        (FakeClient([LLMTimeout("slow", 10000)]), "LLM_TIMEOUT"),                          # s2
        (FakeClient([LLMError("down", 80)]), "LLM_ERROR"),                                 # s3
        (FakeClient([DRAFT.replace("추정치로", "추정치로 적절한")]), "VALIDATION_FAILED"),  # s4 금지 어휘
        (FakeClient([DRAFT.replace("{{risk_basis}}", "{{not_in_input}}")]), "VALIDATION_FAILED"),  # s4 목록에 없는 키
    ],
    ids=["s2_timeout", "s3_call_error", "s4_banned_word", "s4_unknown_key"],
)
def test_s2_to_s4_failure_is_200_failed_without_draft(real_client, llm, reason):
    """실패는 예외가 아니라 200 + FAILED. Spring이 기본 문장으로 대체한다."""
    res = _post(real_client, llm)
    assert res.status_code == 200
    body = res.json()
    assert (body["validation"], body["failReason"], body["draft"]) == ("FAILED", reason, None)
    assert body["latencyMs"] >= 1 and body["costUsd"] >= 0


def test_s7_real_mode_without_key_is_501(real_client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    res = real_client.post("/engine/insight", json=INSIGHT_REQUEST)
    assert (res.status_code, res.json()["code"]) == (501, "LLM_NOT_CONFIGURED")


def test_s8_not_ok_result_is_422(real_client):
    body = {**INSIGHT_REQUEST, "input": {**INSIGHT_REQUEST["input"], "placeholders": ["horizon_days"]}}
    res = _post(real_client, FakeClient([DRAFT]), body)
    assert (res.status_code, res.json()["code"]) == (422, "INVALID_INPUT")
