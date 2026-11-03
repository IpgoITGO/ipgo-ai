"""기본 문장 기준 구현과 기대 문장 fixture (#44). Spring 구현과 대조할 기준이다. 기대값은 이석형이 확인한다."""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from insight.prompt import KEY_DESCRIPTIONS
from insight.validator import check_draft
from schemas.engine import ProductResult
from tests.insight_fallback import PIECES, PLACEHOLDER_RE, Context, build_fallback, placeholder_values

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests" / "fixtures" / "insight_fallback.json").read_text(encoding="utf-8"))
DOC = (ROOT / "docs" / "ai-insight" / "prompt-and-fallback.md").read_text(encoding="utf-8")
# 6장 "상태별 예시 결과"의 예시 이름 → fixture 사례
DOC_EXAMPLES = {"정상": "normal_duplicate", "확인 필요": "low_grade", "판단 보류": "insufficient_sample",
                "산출 불가": "not_calculable", "입고 불필요": "not_needed"}


def _load(case) -> tuple[ProductResult, Context]:
    c = case["context"]
    ctx = Context(as_of_date=date.fromisoformat(c["asOfDate"]) if c["asOfDate"] else None,
                  method_fallback=c["methodFallback"], risk_basis=c["riskBasis"],
                  no_date_receipt_qty=c["noDateReceiptQty"])
    return ProductResult.model_validate(case["result"]), ctx


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_fixture_matches_reference(case):
    fb = build_fallback(*_load(case))
    expected = case["expected"]
    if expected is None:
        assert fb is None  # 시나리오 13: 문장 없이 비교표만
        return
    assert fb is not None
    assert (fb.template, fb.sentences) == (expected["template"], expected["sentences"])


def test_doc_examples_match_fixture():
    """#4 문서 6장 예시 문장과 fixture가 글자까지 같다. 6장을 고치면 fixture도 같이 고친다."""
    examples = dict(re.findall(r"<summary>(.+?) — .*?</summary>\s+(.+?)\s+</details>", DOC, flags=re.S))
    assert set(examples) == set(DOC_EXAMPLES)
    by_id = {c["id"]: c for c in CASES}
    for name, case_id in DOC_EXAMPLES.items():
        assert " ".join(by_id[case_id]["expected"]["sentences"]) == examples[name], name


def test_pieces_are_doc_templates():
    """조각은 6-2 표의 template 칸과 같은 글이고 조각 수도 같다 (`{{키}}` 표기만 다름)."""
    section = DOC.split("### 6-2.")[1].split("### 6-3.")[0]
    rows = re.findall(r"^\| \d+ \| [^|]+ \| (.+?) \| [^|]+ \|$", section, re.M)
    doc = {re.sub(r"`(\{\{\w+\}\})`", r"\1", m) for m in rows}
    assert len(rows) == len(PIECES)
    for piece in PIECES.values():
        assert piece in doc, piece


@pytest.mark.parametrize("case", [c for c in CASES if c["expected"]], ids=lambda c: c["id"])
def test_template_passes_draft_rules(case):
    """기본 문장 template도 LLM 초안과 같은 규칙을 지킨다: 숫자 · 방향 · 평가 어휘는 자리 표시자에만."""
    r, ctx = _load(case)
    template = " ".join(case["expected"]["template"])
    result = check_draft(template, placeholder_values(r, ctx))
    assert [v for v in result.violations if v.code != "format"] == []  # 문장 수는 LLM 초안 기준(2~3)이라 제외


def test_keys_use_contract_names():
    """기본 문장의 자리 표시자 이름은 prompt가 LLM에 주는 이름과 같다 (0002 3-4, #4 문서 5장)."""
    keys = {k for piece in PIECES.values() for k in PLACEHOLDER_RE.findall(piece)}
    keys = {k.replace("_X", "_p80") for k in keys}
    # 계산 불가 사유는 LLM을 부르지 않아 없음. no_date_receipts는 prompt 설명이 아직 없음 (#4 문서 7장)
    assert keys - set(KEY_DESCRIPTIONS) == {"reason", "no_date_receipts"}


@pytest.mark.parametrize("case", [c for c in CASES if c["expected"]], ids=lambda c: c["id"])
def test_sentence_shape(case):
    """U4 미정: 배열이면 조각마다 완결된 한 문장, 하나의 글이면 공백으로 이은 것."""
    fb = build_fallback(*_load(case))
    assert fb is not None and "{{" not in fb.text
    assert all(re.fullmatch(r"[^\n]+[.]", s) for s in fb.sentences)
    assert fb.text == " ".join(fb.sentences)


def test_missing_value_means_table_only():
    """조각에 필요한 값이 하나라도 없으면 문장을 만들지 않는다 (6장, 시나리오 13)."""
    r, ctx = _load(next(c for c in CASES if c["id"] == "normal_duplicate"))
    p120 = r.scenarios[2].model_copy(update={"post_arrival_risk": None})
    assert build_fallback(r.model_copy(update={"scenarios": [*r.scenarios[:2], p120]}), ctx) is None


def test_method_reason_only_when_fallback():
    """산식 사유는 methodFallback일 때만 (0002 3-3). methodReason은 대체하지 않아도 값이 있다."""
    r, ctx = _load(next(c for c in CASES if c["id"] == "low_grade"))
    assert r.method_reason
    fb = build_fallback(r, Context(method_fallback=False))
    assert fb is not None and not any("이 산식을 쓴 이유" in s for s in fb.sentences)


@pytest.mark.parametrize(
    ("ending_p80", "ending_p100", "shown_p80", "shown_p100", "direction"),
    [(19.6, 20.4, "19", "20", "줄어듭니다"),
     (19.1, 19.9, "19", "19", "같습니다"),
     (20.4, 19.6, "20", "19", "늘어납니다")],
)
def test_direction_uses_shown_stock(ending_p80, ending_p100, shown_p80, shown_p100, direction):
    """Spring 비교표처럼 재고를 내린 뒤 방향을 비교한다 (6-4, DisplayQuantities.stock)."""
    r, ctx = _load(next(c for c in CASES if c["id"] == "insufficient_sample"))
    p80 = r.scenarios[0].model_copy(update={"expected_ending_stock": ending_p80})
    p100 = r.scenarios[1].model_copy(update={"expected_ending_stock": ending_p100})
    v = placeholder_values(r.model_copy(update={"scenarios": [p80, p100, r.scenarios[2]]}), ctx)
    assert (v["ending_p80"], v["ending_p100"], v["dir_ending_p80_end"]) == (shown_p80, shown_p100, direction)


def test_decimal_quantities_match_spring_display():
    """시작 재고는 내림, 예상 출고량과 범위는 올림한다 (DisplayQuantities.stock/demand)."""
    r, ctx = _load(next(c for c in CASES if c["id"] == "low_grade"))
    r = r.model_copy(update={"start_stock_qty": 30.8, "forecast_outbound_qty": 96.1,
                             "forecast_range_low": 70.1, "forecast_range_high": 125.1})
    v = placeholder_values(r, ctx)
    assert (v["start_stock"], v["forecast_outbound"], v["forecast_range_low"], v["forecast_range_high"]) == (
        "30", "97", "71", "126")
    fb = build_fallback(r, ctx)
    assert fb is not None
    assert "현재 재고 30개" in fb.text and "97개가 팔릴" in fb.text
    assert "71~126개" in fb.text


@pytest.mark.parametrize(
    ("risk_p80", "risk_p100", "shown_p80", "shown_p100", "direction"),
    [(0.056, 0.064, "6%", "6%", "같습니다"),
     (0.054, 0.064, "5%", "6%", "낮아집니다"),
     (0.066, 0.064, "7%", "6%", "높아집니다")],
)
def test_risk_direction_keeps_percent_rounding(risk_p80, risk_p100, shown_p80, shown_p100, direction):
    """수량의 내림·올림을 비율에는 적용하지 않고 표시된 정수 %로 비교한다."""
    r, ctx = _load(next(c for c in CASES if c["id"] == "pooled_estimated_custom"))
    p80 = r.scenarios[0].model_copy(update={"post_arrival_risk": risk_p80})
    p100 = r.scenarios[1].model_copy(update={"post_arrival_risk": risk_p100})
    v = placeholder_values(r.model_copy(update={"scenarios": [p80, p100, *r.scenarios[2:]]}), ctx)
    assert (v["risk_post_p80"], v["risk_post_p100"], v["dir_risk_post_p80_end"]) == (
        shown_p80, shown_p100, direction)


def test_does_not_mutate_result():
    case = next(c for c in CASES if c["id"] == "pooled_estimated_custom")
    r, ctx = _load(case)
    before = r.model_dump()
    build_fallback(r, ctx)
    assert r.model_dump() == before
