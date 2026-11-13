"""합성 리허설의 문장 결함과 실패 진단 회귀. 실제 공급자 호출 없이 검사한다."""
import json
import logging

import pytest

from insight.llm import FakeClient, LLMResult
from insight.prompt import PROMPT_VERSION, build_prompt
from insight.service import generate_insight
from insight.validator import PLACEHOLDER_RE, check_draft
from schemas.insight import FailReason, InsightInput, Validation

# 실제 합성 리허설에서 PASSED였던 초안. 숫자·계정·실제 상품 정보는 들어 있지 않다.
REHEARSAL_DRAFT = (
    "{{method}} 기준 권장 입고량은 {{qty_p100}}개이며, 다음 입고 때 {{ending_p100}}개가 남습니다. "
    "{{label_p80}}인 {{qty_p80}}개로 {{action_p80}} 다음 입고 때 남는 재고는 "
    "{{ending_p80}}개로 {{dir_ending_p80_end}}. "
    "도착 전 품절 위험은 수량과 관계없이 산출되지 않음(표본 부족입니다"
    "(현재 재고 {{start_stock}}개는 기준일 {{as_of_date}} 이후 출고를 예측한 추정값)."
)
SENTENCES = [
    "{{method}} 기준 권장 입고량은 {{qty_p100}}개이며, 다음 입고 때 {{ending_p100}}개가 남습니다.",
    "{{label_p80}}인 {{qty_p80}}개로 {{action_p80}} 다음 입고 때 남는 재고는 "
    "{{ending_p80}}개로 {{dir_ending_p80_end}}.",
    "품절 위험은 과거 예측 표본이 부족해 산출하지 않았습니다"
    "(현재 재고 {{start_stock}}개는 기준일 {{as_of_date}} 이후 출고를 예측한 추정값).",
]


def _input():
    return InsightInput(
        sku_label="상품 A", recommended="P100", risk_status="INSUFFICIENT_SAMPLE",
        placeholders=["method", "qty_p100", "qty_p80", "qty_p120", "ending_p100", "ending_p80", "ending_p120",
                      "start_stock", "as_of_date"],
        directions={"ending_p80_vs_p100": "같음", "ending_p120_vs_p100": "높음"},
        start_stock_estimated=True,
    )


class ResultClient:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        return self.result


def test_rehearsal_unclosed_parenthesis_is_rejected():
    result = check_draft(REHEARSAL_DRAFT, PLACEHOLDER_RE.findall(REHEARSAL_DRAFT), ["qty_p100"])
    assert result.codes == {"format"}
    assert result.violations[0].detail == "문장 안에서 열고 닫는 괄호가 맞지 않음"


@pytest.mark.parametrize("note", ["(추정값", "추정값)", ")(추정값", "((추정값)"])
def test_each_sentence_requires_parenthesis_pairs(note):
    text = "권장 입고량은 {{qty_p100}}개입니다" + note + ". 품절 위험은 산출하지 않았습니다."
    assert check_draft(text, ["qty_p100"], ["qty_p100"]).codes == {"format"}


def test_parentheses_cannot_be_closed_in_another_sentence():
    text = "권장 입고량은 {{qty_p100}}개입니다(추정값. 품절 위험은 산출하지 않았습니다)."
    assert not check_draft(text, ["qty_p100"], ["qty_p100"]).ok


@pytest.mark.parametrize("note", ["", "(추정값)", "(예측한 재고(추정값))"])
def test_balanced_notes_keep_valid_drafts(note):
    text = "권장 입고량은 {{qty_p100}}개입니다" + note + ". 품절 위험은 산출하지 않았습니다."
    assert check_draft(text, ["qty_p100"], ["qty_p100"]).ok


def test_low_sample_prompt_uses_complete_sentence_and_closed_note():
    prompt = build_prompt(_input())
    assert "산출되지 않음(표본 부족)" not in prompt.user
    assert "과거 예측 표본이 부족해 산출하지 않았습니다." in prompt.user
    assert "괄호는 같은 문장 안에서 열고 닫습니다." in prompt.user
    assert "문장 조각으로 복사하지 말고 완결된 존댓말" in prompt.user
    assert "현재 재고 {{start_stock}}개는 기준일 {{as_of_date}} 이후 출고를 예측한 추정값)" in prompt.user
    assert PROMPT_VERSION == "prompt-2026.11.13-v0.8"


def test_fluent_low_sample_draft_keeps_metadata_and_calls_once():
    client = FakeClient([json.dumps({"sentences": SENTENCES}, ensure_ascii=False)])
    response = generate_insight(_input(), client)
    assert response.validation == Validation.PASSED
    assert response.fail_reason is None
    assert response.draft == " ".join(SENTENCES)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("raw", "truncated", "detail"),
    [
        ("not JSON", False, "format"),
        (json.dumps({"sentences": SENTENCES}), True, "truncated"),
        (json.dumps({"sentences": ["권장 입고량입니다.", "품절 위험은 산출하지 않았습니다."]}), False, "missing_key"),
        (json.dumps({"sentences": ["권장 입고량은 7개입니다.", "품절 위험은 산출하지 않았습니다."]}),
         False, "raw_number"),
        (json.dumps({"sentences": [*SENTENCES[:2], REHEARSAL_DRAFT.split(". ")[-1]]}),
         False, "format"),
    ],
)
def test_failed_validation_is_logged_without_root_info_configuration(caplog, raw, truncated, detail):
    # 운영 컨테이너는 root INFO 설정이 없었다. caplog.at_level(INFO)로 이를 가리지 않는다.
    client = ResultClient(LLMResult(raw, "fake-model", 12, 8, 20, 0.0001, truncated))
    response = generate_insight(_input(), client)
    assert response.validation == Validation.FAILED
    assert response.fail_reason == FailReason.VALIDATION_FAILED
    assert response.draft is None
    assert response.cost_usd == 0.0001
    assert client.calls == 1
    record, = [r for r in caplog.records if r.name == "insight.service"]
    assert record.levelno == logging.WARNING
    assert f"detail={detail}" in record.getMessage()
    assert "권장 입고량" not in record.getMessage()
    assert "상품 A" not in record.getMessage()
    assert "{{" not in record.getMessage()
