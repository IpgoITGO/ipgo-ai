"""AI-01 예측 모델(LGBM19) 결과용 prompt 준비 (#122). 기존 A · B prompt(insight/prompt.py)와 따로 둔다.

- 아직 어디에서도 부르지 않는다. 모델 결과의 설명은 미생성이 현재 결정이다 (#122, PR #128).
- 지침 글은 insight/model_system_prompt.txt. 바꾸면 MODEL_PROMPT_VERSION을 올리고 다시 측정한다.
- 값은 보내지 않는다. 자리 표시자 이름과 설명만 보낸다 (AI-03).
- 품절 위험은 이 방식에서 산출하지 않는다. '표본 부족'으로 쓰지 않는다 (docs/model19-integration.md).
- 입력 계약에 모델 표시 값이 정해지기 전이라, 목표 재고 자리 표시자(target_stock)가 있는 입력만 받는다.
"""
from pathlib import Path

from insight.prompt import KEY_DESCRIPTIONS, SCENARIOS, InsightInputError, Prompt, allowed_keys
from schemas.base import RiskStatus
from schemas.insight import InsightInput

MODEL_PROMPT_VERSION = "model-prompt-2026.11.17-v0.1-local"
MODEL_SYSTEM_PROMPT = (Path(__file__).parent / "model_system_prompt.txt").read_text(encoding="utf-8").rstrip("\n")

MODEL_KEY = "target_stock"
MODEL_KEY_DESCRIPTIONS = KEY_DESCRIPTIONS | {
    MODEL_KEY: "예측 모델이 정한 목표 재고(개, 안전 여유 포함)",
    "method": "적용한 예측 방식 이름",
    "forecast_outbound": "커버 기간 예상 출고량(개)",
}
BANNED_PREFIXES = ("risk_", "dir_risk_", "forecast_range_")
BANNED_KEYS = {"multiplier", "formula_desc", "method_reason"}  # A · B 산식 전제의 키


def _compare_scenario(keys: set[str]) -> str | None:
    """비교 문장에 쓸 시나리오 하나. P80 → P120 → CUSTOM 순서로 방향 키가 있는 첫 시나리오."""
    return next((x for x in SCENARIOS if x != "p100" and f"dir_ending_{x}_end" in keys), None)


def build_model_prompt(inp: InsightInput) -> Prompt:
    """예측 모델 결과의 InsightInput으로 prompt를 만든다. 값은 넣지 않는다."""
    given = set(inp.placeholders)
    required = {MODEL_KEY, "method", "qty_p100"} | {
        f"ending_{x}" for x in SCENARIOS if f"qty_{x}" in given
    }
    if missing := required - given:
        raise InsightInputError("예측 모델 결과에 필요한 자리 표시자가 없어요: " + ", ".join(sorted(missing)))
    allowed = [k for k in allowed_keys(inp) if k not in BANNED_KEYS]
    keys = set(allowed)
    # directions에서 생성된 위험 방향 키도 LLM에 전달하기 전에 거절한다.
    if inp.risk_status == RiskStatus.OK or any(
        k.startswith(BANNED_PREFIXES) or k in BANNED_KEYS for k in given | keys
    ):
        raise InsightInputError("예측 모델 결과에는 위험 · 범위 · 여유 배수 자리 표시자를 보낼 수 없어요")
    compare = _compare_scenario(keys)

    lines = [f"상품: {inp.sku_label}", "", "[입력 — 시나리오 결과 구조]", "계산 상태: NORMAL",
             "적용 예측 방식: {{method}}", "품절 위험: 이 예측 방식에서는 산출하지 않음"]
    if "planned_receipts" in keys:
        lines.append("기존 발주 물량(도착 예정): {{planned_receipts}}")
    checks = []
    if inp.start_stock_estimated and {"start_stock", "as_of_date"} <= keys:
        checks.append("현재 재고 {{start_stock}}개는 기준일 {{as_of_date}} 이후 출고를 예측한 추정값")
    if inp.has_no_date_receipts and "no_date_receipts" in keys:
        checks.append("도착 예정일 없는 발주 {{no_date_receipts}}개는 계산에 넣지 않음")
    if checks:
        lines.append("셀러가 확인할 사항(지침 4) — 마지막 문장 끝 마침표 앞에 덧붙일 글: (" + ", ".join(checks) + ")")
    scenarios = [x for x in SCENARIOS if f"qty_{x}" in keys]
    lines.append("설명할 시나리오(중복 제거 후): " + ", ".join(scenarios))
    for x in scenarios:
        parts = [f"입고량 {{{{qty_{x}}}}}", f"다음 입고 때 잔여 {{{{ending_{x}}}}}"]
        if x != "p100":
            parts.append(f"권장량 대비 {{{{action_{x}}}}}")
            has_dir = f"dir_ending_{x}_end" in keys
            parts.append(f"잔여 재고 {{{{dir_ending_{x}_end}}}}" if has_dir else "잔여 재고 비교 불가")
        lines.append(f"  - {x} (명칭: {{{{label_{x}}}}}): " + ", ".join(parts))
    lines.append(f"비교 문장에 쓸 시나리오: {compare}" if compare
                 else "비교 문장에 쓸 시나리오: 없음 (첫 문장과 셋째 문장만 씁니다)")
    lines += ["", "[자리 표시자 목록 — 이 목록만 사용]"]
    lines += [f"  {{{{{k}}}}} — {MODEL_KEY_DESCRIPTIONS.get(k, '계산 결과 값')}" for k in allowed]
    lines += ["", '위 규칙에 따라 {"sentences": [...]} JSON 으로 AI 인사이트 문장을 작성하세요.']
    return Prompt(MODEL_SYSTEM_PROMPT, "\n".join(lines), allowed, ["qty_p100"])
