"""#122: 예측 모델(LGBM19) 결과용 prompt. 기존 A · B prompt와 따로 두고, 활성화 전에는 호출하지 않는다."""
import json
import re

import pytest

from insight.model_prompt import MODEL_PROMPT_VERSION, MODEL_SYSTEM_PROMPT, build_model_prompt
from insight.prompt import PROMPT_VERSION, SYSTEM_PROMPT, InsightInputError, draft_text
from insight.validator import PLACEHOLDER_RE, check_draft
from schemas.base import RiskStatus
from schemas.insight import InsightInput

BASE = ["method", "horizon_days", "start_stock", "planned_receipts", "forecast_outbound", "target_stock"]
NO_RISK_LINE = "품절 위험: 이 예측 방식에서는 산출하지 않음"
NO_RISK_SENTENCE = "이 예측 방식에서는 품절 위험을 산출하지 않았습니다."


def _keys(*scenarios: str) -> list[str]:
    keys = list(BASE)
    for x in scenarios:
        keys += [f"{name}_{x}" for name in ("qty", "depletion", "ending", "unmet")]
        if x != "p100":
            keys.append(f"diff_{x}")
    return keys


def _input(*scenarios: str, extra: tuple[str, ...] = (), drop: tuple[str, ...] = (), **changes) -> InsightInput:
    scenarios = scenarios or ("p80", "p100", "p120")
    fields = dict(
        sku_label="상품 A", recommended="P100", risk_status="NOT_COMPUTED", method="LGBM19",
        placeholders=[k for k in _keys(*scenarios) + list(extra) if k not in drop],
        directions={f"ending_{x}_vs_p100": "낮음" if x == "p80" else "높음" for x in scenarios if x != "p100"},
    )
    # prompt 방어 검사에는 wire 검증을 통과할 수 없는 조합도 직접 전달한다.
    return InsightInput(**fields).model_copy(update=changes)


def _example() -> str:
    line = next(ln for ln in MODEL_SYSTEM_PROMPT.splitlines() if ln.startswith('{"sentences"'))
    return draft_text(line)


def test_model_prompt_is_separate_from_ab_prompt():
    assert MODEL_SYSTEM_PROMPT != SYSTEM_PROMPT
    assert MODEL_PROMPT_VERSION != PROMPT_VERSION
    assert PROMPT_VERSION == "prompt-2026.11.13-v0.8"  # 기존 A · B prompt는 바뀌지 않는다


def test_prompt_states_risk_is_not_computed_without_ab_wording():
    prompt = build_model_prompt(_input())
    assert prompt.system == MODEL_SYSTEM_PROMPT
    assert NO_RISK_LINE in prompt.user
    for word in ("표본", "여유 배수", "계산식", "추정치"):
        assert word not in prompt.user
    assert prompt.required == ["method", "target_stock", "qty_p100", "ending_p100"]
    assert "target_stock" in prompt.allowed
    banned = [k for k in prompt.allowed if k.startswith(("risk_", "dir_risk_")) or k in ("multiplier", "formula_desc")]
    assert not banned


@pytest.mark.parametrize("scenarios", [
    ("p80", "p100", "p120"), ("p100", "p120"), ("p100", "custom"), ("p100",),
])
def test_prompt_uses_only_listed_keys(scenarios):
    prompt = build_model_prompt(_input(*scenarios, start_stock_estimated=True, has_no_date_receipts=True,
                                       extra=("as_of_date", "no_date_receipts")))
    assert set(PLACEHOLDER_RE.findall(prompt.user)) <= set(prompt.allowed)


def test_example_passes_validator_and_has_no_sample_wording():
    prompt = build_model_prompt(_input())
    text = _example()
    assert text is not None
    assert check_draft(text, prompt.allowed, prompt.required).ok
    assert text.endswith(NO_RISK_SENTENCE)
    assert "표본" not in text and "risk_" not in text


@pytest.mark.parametrize(("scenarios", "expected"), [
    (("p80", "p100", "p120"), "비교 문장에 쓸 시나리오: p80"),
    (("p100", "p120"), "비교 문장에 쓸 시나리오: p120"),          # 80%가 권장량과 겹쳐 빠진 경우
    (("p100", "custom"), "비교 문장에 쓸 시나리오: custom"),
    (("p100",), "비교 문장에 쓸 시나리오: 없음"),
])
def test_one_comparison_scenario_is_chosen(scenarios, expected):
    assert expected in build_model_prompt(_input(*scenarios)).user


def test_seller_checks_follow_metadata_flags():
    plain = build_model_prompt(_input())
    assert "셀러가 확인할 사항" not in plain.user

    both = build_model_prompt(_input(start_stock_estimated=True, has_no_date_receipts=True,
                                     extra=("as_of_date", "no_date_receipts")))
    line = next(ln for ln in both.user.splitlines() if ln.startswith("셀러가 확인할 사항"))
    assert line.count("(") == 2 and line.endswith(")")  # '(지침 4)'와 덧붙일 괄호 하나
    assert "{{start_stock}}" in line and "{{as_of_date}}" in line and "{{no_date_receipts}}" in line

    # 표시만 있고 키가 없으면 안내를 만들지 않는다
    assert "셀러가 확인할 사항" not in build_model_prompt(_input(start_stock_estimated=True)).user


@pytest.mark.parametrize("inp", [
    pytest.param(dict(drop=("target_stock",)), id="목표 재고 없음"),
    pytest.param(dict(drop=("qty_p100",)), id="권장량 없음"),
    pytest.param(dict(drop=("method",)), id="방식 이름 없음"),
    pytest.param(dict(drop=("ending_p100",)), id="권장량 잔여 재고 없음"),
    pytest.param(dict(drop=("ending_p80",)), id="80 시나리오 잔여 재고 없음"),
    pytest.param(dict(drop=("ending_p120",)), id="120 시나리오 잔여 재고 없음"),
    pytest.param(dict(risk_status=RiskStatus.OK), id="위험 산출 상태"),
    pytest.param(dict(extra=("multiplier",)), id="여유 배수"),
    pytest.param(dict(extra=("risk_pre",)), id="위험 값"),
    pytest.param(dict(directions={"risk_post_p80_vs_p100": "낮음"}), id="80 위험 방향"),
    pytest.param(dict(directions={"risk_post_p120_vs_p100": "낮음"}), id="120 위험 방향"),
    pytest.param(dict(directions={"risk_post_custom_vs_p100": "낮음"}), id="직접 입력 위험 방향"),
    pytest.param(dict(extra=("method_reason",), method_fallback=True), id="A · B 산식 사유"),
    pytest.param(dict(extra=("forecast_range_low", "forecast_range_high"), risk_status=RiskStatus.LOW_GRADE),
                 id="예상 출고량 범위"),
])
def test_mixed_or_incomplete_input_is_rejected(inp):
    with pytest.raises(InsightInputError):
        build_model_prompt(_input(**inp))


def test_custom_scenario_requires_ending_stock():
    with pytest.raises(InsightInputError, match="ending_custom"):
        build_model_prompt(_input("p100", "custom", drop=("ending_custom",)))


def test_system_prompt_example_is_valid_json_with_three_sentences():
    line = next(ln for ln in MODEL_SYSTEM_PROMPT.splitlines() if ln.startswith('{"sentences"'))
    sentences = json.loads(line)["sentences"]
    assert len(sentences) == 3
    assert all(re.fullmatch(r"[^.!?]+[.!?]", PLACEHOLDER_RE.sub("값", s)) for s in sentences)
