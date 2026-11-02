"""AI-01 prompt 만들기 (#43). InsightInput 하나로 LLM에 보낼 prompt와 쓸 수 있는 · 꼭 써야 하는 자리 표시자를 만든다.

- system prompt는 #32 시험 prompt 고정본과 같은 글(insight/system_prompt.txt).
  바꾸면 PROMPT_VERSION을 올리고 다시 측정한다.
- 값은 보내지 않는다. 숫자와 방향 문구는 Spring이 채운다 (#24). LLM에는 자리 표시자 이름과 설명만 간다 (AI-03).
- 문구 키(label · action · dir)는 엔진 목록에 없어도 시나리오와 directions에서 만든다 (#4 문서 5장 †).
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from schemas.base import RiskStatus
from schemas.insight import InsightInput

PROMPT_VERSION = "prompt-2026.11.02-v0.5"  # v0.4(참조 구현) + #32 측정 보강 (#4 문서 3장)
SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.txt").read_text(encoding="utf-8").rstrip("\n")

SCENARIOS = ("p80", "p100", "p120", "custom")  # 계약 ScenarioType을 소문자로 (#4 문서 5장)
SCENARIO_NAMES = {"p80": "권장량의 80%", "p100": "권장량", "p120": "권장량의 120%", "custom": "직접 입력한 수량"}
ACTION_WORDS = {"p80": "줄이면", "p120": "늘리면"}  # custom은 수량에 따라 달라 값을 보여 주지 않음
DIRECTION_RE = re.compile(r"^(ending|risk_post)_(p80|p120|custom)_vs_p100$")  # directions 키 (0002 3-3)

BASE_KEYS = {
    "as_of_date": "기준일(수불이 확정된 마지막 날)",
    "horizon_days": "커버 기간(일) = 입고 주기 + 리드타임",
    "start_stock": "현재(기준일) 재고(개)",
    "planned_receipts": "커버 기간 안에 도착하는 기존 발주 물량(개)",
    "method": "적용한 산식 이름",
    "formula_desc": "적용한 산식 설명",
    "method_reason": "이 산식을 쓴 이유(평가 결과 또는 A를 못 쓴 사유) — 근거 문장에 넣을 것",
    "forecast_outbound": "커버 기간 예상 출고량(개, 배수 적용 전)",
    "multiplier": "여유 배수",
    "risk_pre": "새 물량 도착 전 품절 위험(모든 시나리오 공통, 과거 오차 기준 추정치)",
    "risk_sample_size": "위험 추정에 쓴 과거 예측 오차 표본 수(건)",
    "risk_basis": "위험 기준 — 위험을 언급하는 문장에 함께 쓸 것",
}
TAIL_KEYS = {
    "forecast_range_low": "커버 기간 예상 출고량 범위의 아래쪽(개)",
    "forecast_range_high": "커버 기간 예상 출고량 범위의 위쪽(개)",
}


def _scenario_keys(x: str) -> dict[str, str]:
    """시나리오 하나의 자리 표시자와 설명. 순서는 고정본과 같다."""
    n = SCENARIO_NAMES[x]
    keys = {
        f"label_{x}": f"{n} 시나리오의 명칭",
        f"qty_{x}": f"{n} 시나리오의 입고량(개)",
        f"depletion_{x}": f"{n}일 때 재고 소진 예상일",
        f"ending_{x}": f"{n}일 때 다음 입고 시점 잔여 재고(개)",
        f"unmet_{x}": f"{n}일 때 커버 기간 중 미충족 수요(개, 판매 손실)",
        f"risk_post_{x}": f"{n}일 때 새 물량 도착 후 품절 위험(추정치)",
    }
    if x == "p100":
        return keys
    action = f"'{ACTION_WORDS[x]}'" if x in ACTION_WORDS else "'줄이면' · '늘리면' · '그대로 두면' 중 하나"
    return keys | {
        f"action_{x}": f"권장량 대비 {n}으로 수량을 {action} (셀러의 행동)",
        f"diff_{x}": f"권장량과 {n} 입고량의 차이(개)",
        f"dir_ending_{x}_connect": f"권장량 대비 {n}일 때 잔여 재고 방향, 문장 중간용 (줄지만/늘지만/같고)",
        f"dir_ending_{x}_end": f"권장량 대비 {n}일 때 잔여 재고 방향, 문장 끝용 (줄어듭니다/늘어납니다/같습니다)",
        f"dir_risk_post_{x}_connect": f"권장량 대비 {n}일 때 도착 후 품절 위험 방향, 문장 중간용",
        f"dir_risk_post_{x}_end": f"권장량 대비 {n}일 때 도착 후 품절 위험 방향, 문장 끝용 "
                                  "(높아집니다/낮아집니다/같습니다)",
    }


KEY_DESCRIPTIONS = BASE_KEYS | {k: v for x in SCENARIOS for k, v in _scenario_keys(x).items()} | TAIL_KEYS

# system prompt의 상태별 작성 예시 (가) 정상 · (나) 판단 보류 · (다) 확인 필요에 나오는 자리 표시자
_EXAMPLES = [ln for ln in SYSTEM_PROMPT.splitlines() if ln.startswith('{"sentences"')]
EXAMPLE_KEYS = {
    status: re.findall(r"\{\{([a-z0-9_]+)\}\}", line)
    for status, line in zip((RiskStatus.OK, RiskStatus.INSUFFICIENT_SAMPLE, RiskStatus.LOW_GRADE), _EXAMPLES,
                            strict=True)
}
SCENARIO_SUFFIX_RE = re.compile(r"_(p80|p120|custom)(_|$)")  # 중복으로 빠질 수 있는 시나리오의 키


class InsightInputError(ValueError):
    """LLM을 부를 수 없는 입력. Spring은 정상(OK) 결과만 부른다 (#24)."""


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    allowed: list[str]   # 쓸 수 있는 자리 표시자 (목록 순서)
    required: list[str]  # 꼭 써야 하는 자리 표시자


def allowed_keys(inp: InsightInput) -> list[str]:
    """엔진이 준 자리 표시자 + 시나리오 · directions로 만든 문구 키. 목록 순서로 돌려준다."""
    keys = set(inp.placeholders)
    scenarios = [x for x in SCENARIOS if f"qty_{x}" in keys]
    keys |= {f"label_{x}" for x in scenarios}
    keys |= {f"action_{x}" for x in scenarios if x != "p100"}
    if "method" in keys:
        keys.add("formula_desc")  # 산식별 고정 문구 (0002 3-4)
    if inp.risk_status == RiskStatus.OK and "risk_post_p100" in keys:
        keys.add("risk_basis")  # riskBasis(SKU · POOLED) 문구 (0002 3-8)
    for d in inp.directions:
        if m := DIRECTION_RE.match(d):
            keys |= {f"dir_{m[1]}_{m[2]}_connect", f"dir_{m[1]}_{m[2]}_end"}
    known = [k for k in KEY_DESCRIPTIONS if k in keys]
    return known + sorted(keys - set(known))


def required_keys(inp: InsightInput) -> list[str]:
    """권장량은 늘, 위험은 산출됐을 때, 범위는 정확도가 낮을 때 (#4 문서 4장, 고정본 required와 같음)."""
    req = ["qty_p100"]
    if inp.risk_status == RiskStatus.OK:
        req += [k for k in ("risk_post_p100", "risk_pre") if k in inp.placeholders]
    elif inp.risk_status == RiskStatus.LOW_GRADE:
        req += ["forecast_range_low", "forecast_range_high"]
    return req


def _scenario_line(x: str, keys: set[str], risk: bool) -> str:
    """시나리오 한 줄. 목록에 없는 키는 쓰지 않는다 (LLM이 따라 쓰면 unknown_key)."""
    def k(name: str) -> str:
        return f"{{{{{name}_{x}}}}}"

    parts = [f"입고량 {k('qty')}"]
    parts += [f"{label} {k(name)}" for label, name in (("소진일", "depletion"), ("다음 입고 때 잔여", "ending"))
              if f"{name}_{x}" in keys]
    if not risk:
        parts.append("도착 후 위험 산출되지 않음")
    elif f"risk_post_{x}" in keys:
        parts.append(f"도착 후 위험 {k('risk_post')}")
    line = f"  - {x} (명칭: {k('label')}): " + ", ".join(parts)
    if x == "p100":
        return line
    line += f"; 권장량 대비 {k('action')}"
    line += (f", 잔여 재고 {{{{dir_ending_{x}_end}}}}" if f"dir_ending_{x}_end" in keys else ", 잔여 재고 비교 불가")
    return line + (f", 위험 {{{{dir_risk_post_{x}_end}}}}" if f"dir_risk_post_{x}_end" in keys else ", 위험 비교 불가")


def _missing_notes(status: RiskStatus, keys: set[str], scenarios: list[str]) -> list[str]:
    """엔진 목록에 키가 빠졌을 때만 붙는 안내. 다 있으면 빈 목록이라 측정한 prompt와 같다.

    빠진 키를 그대로 두면 LLM이 작성 예시를 따라 목록에 없는 키를 쓰거나(unknown_key),
    다른 시나리오의 방향 키로 대신 채운다(scenario_mismatch, #43 실제 호출).
    """
    notes = []
    missing = [k for k in EXAMPLE_KEYS[status] if k not in keys and not SCENARIO_SUFFIX_RE.search(k)]
    if missing:
        notes.append("※ 작성 예시에 나오지만 이번 목록에 없는 자리 표시자: "
                     + ", ".join(f"{{{{{k}}}}}" for k in dict.fromkeys(missing)) + ". 이 내용은 빼고 씁니다.")
    no_dir = [f"{x} 잔여 재고" for x in scenarios if x != "p100" and f"dir_ending_{x}_end" not in keys]
    if status == RiskStatus.OK:
        no_dir += [f"{x} 위험" for x in scenarios if x != "p100" and f"dir_risk_post_{x}_end" not in keys]
    if no_dir:
        notes.append("※ 방향 자리 표시자가 없는 비교: " + ", ".join(no_dir)
                     + ". 다른 시나리오의 방향 키로 대신하지 말고 이 방향은 말하지 않습니다.")
    return notes


def build_prompt(inp: InsightInput) -> Prompt:
    """상태 판정(정상 · 확인 필요 · 판단 보류)과 자리 표시자 목록으로 prompt를 만든다. 값은 넣지 않는다."""
    if "qty_p100" not in inp.placeholders:
        raise InsightInputError("권장량 자리 표시자(qty_p100)가 없어요. 정상(OK) 결과만 보낼 수 있어요")
    allowed = allowed_keys(inp)
    keys = set(allowed)
    risk = inp.risk_status == RiskStatus.OK
    status = "계산 상태: NORMAL"
    if inp.risk_status == RiskStatus.LOW_GRADE:
        status += ", 위험 상태: LOW_GRADE (예측 정확도가 낮음. 품절 위험은 말하지 말고 예상 출고량 범위로만 설명)"
    lines = [f"상품: {inp.sku_label}", "", "[입력 — 시나리오 결과 구조]", status]
    if "method" in keys:
        reason = " (사유 있음 → {{method_reason}})" if "method_reason" in keys else ""
        lines.append("적용 산식: {{method}}" + reason)
    if "planned_receipts" in keys:
        lines.append("기존 발주 물량(도착 예정): {{planned_receipts}}")
    if risk:
        lines.append("도착 전 품절 위험(공통): 산출됨 → {{risk_pre}}")
        if "risk_sample_size" in keys:
            lines.append("위험 기준: {{risk_basis}}, 표본 수 {{risk_sample_size}}")
    elif inp.risk_status == RiskStatus.LOW_GRADE:
        lines.append("도착 전 품절 위험(공통): 산출하지 않음(정확도 낮음) → 대신 커버 기간 예상 출고량 범위 "
                     "{{forecast_range_low}}~{{forecast_range_high}}")
    else:
        lines.append("도착 전 품절 위험(공통): 산출되지 않음(표본 부족)")
    scenarios = [x for x in SCENARIOS if f"qty_{x}" in keys]
    lines.append("설명할 시나리오(중복 제거 후): " + ", ".join(scenarios))
    lines += [_scenario_line(x, keys, risk) for x in scenarios]
    lines += _missing_notes(inp.risk_status, keys, scenarios)
    lines += ["", "[자리 표시자 목록 — 이 목록만 사용]"]
    lines += [f"  {{{{{k}}}}} — {KEY_DESCRIPTIONS.get(k, '계산 결과 값')}" for k in allowed]
    lines += ["", '위 규칙에 따라 {"sentences": [...]} JSON 으로 AI 인사이트 문장을 작성하세요.']
    return Prompt(SYSTEM_PROMPT, "\n".join(lines), allowed, required_keys(inp))


def draft_text(raw: str) -> str | None:
    """응답 형식 {"sentences": [...]}을 문장 하나로 잇는다. 형식이 아니면 None."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    sentences = obj.get("sentences") if isinstance(obj, dict) else None
    if not isinstance(sentences, list) or not all(isinstance(s, str) for s in sentences):
        return None
    return " ".join(s.strip() for s in sentences if s.strip())
