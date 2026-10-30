"""문장 초안 검사 (#29). 사례와 기대 사유 코드는 #4 문서 4장 "예" 열에서 가져왔다.

기대값은 이석형이 확인한다 (AI가 고치지 않음).
"""
import ast
import sys
from pathlib import Path

import pytest

from insight.validator import check_draft

# 규칙을 지킨 문장 초안 (0002 3-4 이름, 정상 상태)
PASS = (
    "{{method}} 기준 권장 입고량은 {{qty_p100}}개이며, 다음 입고 때 {{ending_p100}}개가 남고 "
    "도착 후 품절 위험은 {{risk_basis}} 추정치로 {{risk_post_p100}}입니다. "
    "{{label.p80}}인 {{qty_p80}}개로 {{action.p80}} 남는 재고는 {{dir.remain.p80_p100.connect}} "
    "품절 위험은 {{risk_post_p80}}로 {{dir.risk_after.p80_p100.end}}. "
    "새 물량이 도착하기 전까지의 품절 위험은 수량과 관계없이 {{risk_pre}}입니다."
)
ALLOWED = {
    "method", "qty_p100", "ending_p100", "risk_basis", "risk_post_p100", "label.p80", "qty_p80", "action.p80",
    "dir.remain.p80_p100.connect", "dir.risk_after.p80_p100.end", "risk_post_p80", "risk_pre",
}
REQUIRED = ["qty_p100"]


def _check(text: str):
    return check_draft(text, ALLOWED, REQUIRED)


def test_pass():
    assert _check(PASS).violations == []


def test_pass_with_current_doc_names():
    text = (
        "{{formula}} 기준 권장 입고량은 {{qty.s100}}개이며, 도착 후 품절 위험은 {{risk_basis}} 추정치로 "
        "{{risk_after.s100}}입니다. 새 물량이 도착하기 전까지의 품절 위험은 수량과 관계없이 {{risk_before}}입니다."
    )
    allowed = {"formula", "qty.s100", "risk_basis", "risk_after.s100", "risk_before"}
    assert check_draft(text, allowed, ["qty.s100"]).ok


def test_pass_risk_not_computed():
    # #4 문서 3장: 판단 보류는 "표본이 부족해 산출하지 않았습니다"라고만 씀
    text = (
        "권장 입고량은 {{qty_p100}}개이며, 다음 입고 때 {{ending_p100}}개가 남습니다. "
        "품절 위험은 표본이 부족해 산출하지 않았습니다."
    )
    assert _check(text).ok


def test_pass_seller_action():
    # 셀러의 행동(줄이면 · 늘리면)은 방향 어휘가 아님
    text = PASS.replace("{{action.p80}}", "줄이면")
    assert _check(text).ok


@pytest.mark.parametrize(
    ("phrase", "code"),
    [
        ("290개", "raw_number"),
        ("8%", "raw_number"),
        ("80% 시나리오", "raw_number"),
        ("두 배", "raw_number"),
        ("절반", "raw_number"),
        ("열흘", "raw_number"),
        ("일주일", "raw_number"),
        ("높아집니다", "banned_word"),
        ("줄어듭니다", "banned_word"),
        ("낮춰요", "banned_word"),
        ("올라가요", "banned_word"),
        ("증가", "banned_word"),
        ("최적", "banned_word"),
        ("적절", "banned_word"),
        ("안전한", "banned_word"),
        ("추천", "banned_word"),
        ("반드시", "banned_word"),
        ("품절됩니다", "banned_word"),
        ("배수를 낮추세요", "banned_word"),
        ("300개로 권장", "banned_word"),
    ],
)
def test_banned_phrase(phrase, code):
    result = _check(PASS.replace("수량과 관계없이", f"수량과 관계없이 {phrase}"))
    assert code in result.codes


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("{{risk_pre}}", "{{qty_p120}}", "unknown_key"),  # 중복 시나리오
        ("{{risk_pre}}", "{{dir.remain.p100_p100.end}}", "unknown_key"),  # 직접 만든 방향 키
        ("권장 입고량은 {{qty_p100}}개이며", "권장 입고량이 정해졌으며", "missing_key"),
        ("{{risk_basis}} 추정치로 ", "", "banned_word"),  # 위험을 추정치라고 밝히지 않음
        (". {{label.p80}}", ".\n{{label.p80}}", "format"),
        ("{{qty_p100}}개", "**{{qty_p100}}개**", "format"),
        ("{{qty_p100}}", "{qty_p100}", "format"),
    ],
)
def test_violation(old, new, code):
    text = PASS.replace(old, new)
    assert text != PASS
    assert code in _check(text).codes


@pytest.mark.parametrize(
    "text",
    [
        "권장 입고량은 {{qty_p100}}개입니다.",
        PASS + " 결정은 셀러가 합니다.",
    ],
)
def test_sentence_count(text):
    assert "format" in _check(text).codes


def test_validator_uses_standard_library_only():
    path = Path(__file__).resolve().parents[1] / "insight" / "validator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    roots |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert roots <= sys.stdlib_module_names
