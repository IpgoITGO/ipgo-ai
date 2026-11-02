"""AI 인사이트 생성 (#43). 가짜 호출기로만 돈다. 기대값은 이석형이 확인한다."""
import json
import logging
import re

import pytest

from app.main import app
from app.routers.insight import get_llm_client
from insight.benchmark import load_cases
from insight.llm import FakeClient, LLMError, LLMResult, LLMTimeout
from insight.prompt import SYSTEM_PROMPT, InsightInputError, allowed_keys, build_prompt, required_keys
from insight.service import generate_insight
from schemas.base import RiskStatus
from schemas.insight import FailReason, InsightInput, Validation
from tests.payloads import INSIGHT_REQUEST

RISK = {"low_sample": RiskStatus.INSUFFICIENT_SAMPLE, "low_grade": RiskStatus.LOW_GRADE}
PHRASE = re.compile(r"^(label|action|dir)_|^(formula_desc|risk_basis)$")  # 엔진이 아니라 여기서 만드는 키
KEY_LINE = re.compile(r"^  \{\{(\S+)\}\} — (.*)$")


def _fixture_input(case) -> InsightInput:
    """고정본 사례를 엔진이 보낼 InsightInput으로 되돌린다: 숫자 키는 목록, 방향은 directions."""
    dirs = {f"{m[1]}_{m[2]}_vs_p100": "높음" for k in case.allowed
            if (m := re.match(r"^dir_(ending|risk_post)_(\w+?)_end$", k))}
    return InsightInput(sku_label="(이 상품)", recommended="P100", risk_status=RISK.get(case.id, RiskStatus.OK),
                        placeholders=[k for k in case.allowed if not PHRASE.match(k)], directions=dirs)


NAMES_ONLY = [c for c in load_cases()[1] if not c.values]
OK_DRAFT = json.dumps({"sentences": [
    "{{method}} 기준 권장 입고량은 {{qty_p100}}개이며, 다음 입고 때 {{ending_p100}}개가 남고 도착 후 품절 위험은 "
    "{{risk_basis}} 추정치로 {{risk_post_p100}}입니다(표본 {{risk_sample_size}}건).",
    "새 물량이 도착하기 전까지의 품절 위험은 수량과 관계없이 {{risk_pre}}입니다."]}, ensure_ascii=False)


def _normal_input():
    return _fixture_input(next(c for c in NAMES_ONLY if c.id == "pooled"))


def test_system_prompt_is_the_measured_fixture():
    assert SYSTEM_PROMPT == load_cases()[0]  # #32에서 측정한 system prompt와 같은 글


@pytest.mark.parametrize("case", NAMES_ONLY, ids=lambda c: c.id)
def test_keys_match_fixture(case):
    inp = _fixture_input(case)
    assert allowed_keys(inp) == [m[1] for line in case.user.splitlines() if (m := KEY_LINE.match(line))]
    assert required_keys(inp) == case.required


@pytest.mark.parametrize("case", NAMES_ONLY, ids=lambda c: c.id)
def test_key_descriptions_match_fixture(case):
    def described(user):
        return {m[1]: m[2] for line in user.splitlines() if (m := KEY_LINE.match(line))}

    ours, fixture = described(build_prompt(_fixture_input(case)).user), described(case.user)
    fixture.pop("action_custom", None)  # 직접 입력의 행동은 수량에 따라 달라 값을 보여 주지 않음
    ours.pop("action_custom", None)
    assert ours == fixture


def test_prompt_has_no_values_or_direction_words():
    p = build_prompt(InsightInput.model_validate(INSIGHT_REQUEST["input"]))
    assert "높음" not in p.user and "[값:" not in p.user  # 방향 값과 실제 값은 Spring이 채움 (#24)
    assert "{{dir_risk_post_p80_end}}" in p.user and "dir_ending_p120_connect" in p.allowed


def test_prompt_uses_only_listed_keys():
    p = build_prompt(InsightInput.model_validate(INSIGHT_REQUEST["input"]))
    body = "\n".join(line for line in p.user.splitlines() if not line.startswith("※"))  # ※는 쓰지 말라는 안내
    assert set(re.findall(r"\{\{([a-z0-9_]+)\}\}", body)) <= set(p.allowed)  # 엔진이 안 준 소진일 · 잔여 키는 안 씀


@pytest.mark.parametrize("case", NAMES_ONLY, ids=lambda c: c.id)
def test_complete_input_has_no_missing_notes(case):
    assert "※" not in build_prompt(_fixture_input(case)).user  # 측정한 prompt와 같은 조건


def test_missing_keys_are_announced():
    # 키가 빠진 입력: 10/30 실제 호출에서 예시의 {{method}}를 베끼고 80% 문장에 120% 방향 키를 씀
    user = build_prompt(InsightInput.model_validate(INSIGHT_REQUEST["input"])).user
    assert "※ 작성 예시에 나오지만 이번 목록에 없는 자리 표시자: {{method}}, {{ending_p100}}" not in user
    assert "{{method}}" in user and "{{risk_sample_size}}" in user
    assert "※ 방향 자리 표시자가 없는 비교: p80 잔여 재고, p120 위험." in user
    assert "잔여 재고 비교 불가" in user


def test_input_without_recommended_qty_is_rejected():
    inp = InsightInput(sku_label="상품 A", recommended="P100", placeholders=["horizon_days"], directions={},
                       risk_status=RiskStatus.OK)
    with pytest.raises(InsightInputError):
        build_prompt(inp)


class _Truncated:
    model = "gpt-4o-mini"

    def complete(self, system, user, **_):
        return LLMResult(OK_DRAFT, self.model, 100, 400, 50, 0.0003, True)


@pytest.mark.parametrize(
    ("client", "validation", "reason", "latency"),
    [
        (FakeClient([OK_DRAFT], latency_ms=120), Validation.PASSED, None, 120),
        (FakeClient(["문장"]), Validation.FAILED, FailReason.VALIDATION_FAILED, 1),             # JSON 아님
        (_Truncated(), Validation.FAILED, FailReason.VALIDATION_FAILED, 50),                     # 출력 잘림
        (FakeClient([OK_DRAFT.replace("추정치로", "추정치로 적절한")]),
         Validation.FAILED, FailReason.VALIDATION_FAILED, 1),                                    # 금지 어휘
        (FakeClient([OK_DRAFT.replace("{{risk_pre}}", "공통 위험")]),
         Validation.FAILED, FailReason.VALIDATION_FAILED, 1),                                    # 필수 키 누락
        (FakeClient([LLMTimeout("slow", 10000)]), Validation.FAILED, FailReason.LLM_TIMEOUT, 10000),
        (FakeClient([LLMError("down", 80)]), Validation.FAILED, FailReason.LLM_ERROR, 80),
    ],
    ids=["passed", "not_json", "truncated", "banned_word", "missing_key", "timeout", "call_error"],
)
def test_generate_insight(client, validation, reason, latency):
    res = generate_insight(_normal_input(), client)
    assert (res.validation, res.fail_reason, res.latency_ms) == (validation, reason, latency)
    assert (res.draft is not None) == (validation == Validation.PASSED)
    assert res.cost_usd >= 0  # 실패해도 기록 (모르면 0)


def test_llm_gets_only_insight_input():
    client = FakeClient([OK_DRAFT])
    generate_insight(_normal_input(), client)
    (system, user), = client.calls
    assert system == SYSTEM_PROMPT
    assert user.startswith("상품: (이 상품)\n") and "[값:" not in user


def test_log_has_no_prompt_or_draft(caplog):
    with caplog.at_level(logging.INFO, logger="insight.service"):
        generate_insight(_normal_input(), FakeClient([OK_DRAFT]))
    assert "insight passed" in caplog.text
    assert "권장 입고량" not in caplog.text and "상품:" not in caplog.text


@pytest.fixture
def real_client(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    yield client
    app.dependency_overrides.clear()


def test_real_mode_returns_checked_draft(real_client):
    draft = json.dumps({"sentences": ["권장 입고량은 {{qty_p100}}개입니다.",
                                      "품절 위험은 {{risk_post_p100}}로 추정되고 공통 위험은 {{risk_pre}}입니다."]},
                       ensure_ascii=False)
    app.dependency_overrides[get_llm_client] = lambda: FakeClient([draft])
    res = real_client.post("/engine/insight", json=INSIGHT_REQUEST)
    assert res.status_code == 200, res.text
    assert res.json()["validation"] == "PASSED"
    assert res.json()["draft"].startswith("권장 입고량은 {{qty_p100}}개")


def test_real_mode_llm_failure_is_200_failed(real_client):
    app.dependency_overrides[get_llm_client] = lambda: FakeClient([LLMTimeout("slow", 10000)])
    res = real_client.post("/engine/insight", json=INSIGHT_REQUEST)
    assert res.status_code == 200
    assert (res.json()["validation"], res.json()["failReason"]) == ("FAILED", "LLM_TIMEOUT")


def test_real_mode_not_ok_result_is_invalid_input(real_client):
    app.dependency_overrides[get_llm_client] = lambda: FakeClient([OK_DRAFT])
    body = {**INSIGHT_REQUEST, "input": {**INSIGHT_REQUEST["input"], "placeholders": ["horizon_days"]}}
    res = real_client.post("/engine/insight", json=body)
    assert res.status_code == 422
    assert res.json()["code"] == "INVALID_INPUT"


def test_real_mode_without_key_is_explicit_error(real_client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    res = real_client.post("/engine/insight", json=INSIGHT_REQUEST)
    assert res.status_code == 501
    assert res.json()["code"] == "LLM_NOT_CONFIGURED"
