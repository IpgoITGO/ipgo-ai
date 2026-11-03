"""평가 사례: 0002 ProductResult + 계산 결과 밖에서 오는 값(Context). 참조 구현 EngineResult를 대신한다.

- 참조 구현(ai_insight v0.3)의 계산 결과 형식(EngineResult)을 0002 이름으로 옮기는 변환(from_reference)을 둔다.
  참조 구현 samples/ · mock_engine 결과를 그대로 쓰기 위해서다.
- 인사이트 입력(to_input)은 엔진의 to_insight_input()(#10 제외 범위 "별도 Issue")이 생기기 전까지의 대신 함수다.
- 값 넣기 · 숫자 대조 · 기본 문장은 Spring 대신 #44 기준 구현(tests/insight_fallback.py)을 쓴다.
"""

import re
from datetime import date

from pydantic import BaseModel

from schemas.base import Method, ResultStatus, RiskStatus, ScenarioType
from schemas.engine import ProductResult
from schemas.insight import InsightInput
from tests.insight_fallback import Context, _date, _int, _round, placeholder_values

PHRASE_RE = re.compile(
    r"^(label|action|dir)_|^(formula_desc|risk_basis|reason)$"
)  # 엔진이 아니라 FastAPI · Spring이 만드는 키
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
SCENARIO_NAMES = {
    "p80": "권장량의 80%",
    "p100": "권장량",
    "p120": "권장량의 120%",
    "custom": "직접 입력한 수량",
}  # insight.prompt
EXPERIMENT_TYPES = (ScenarioType.P80, ScenarioType.P100, ScenarioType.P120)  # 참조 구현 table.EXPERIMENT_KEYS


class EvalCase(BaseModel):
    """참조 구현 EngineResult 한 건에 해당한다. calc_id → id, sku.name → name."""

    id: str
    name: str | None = None
    as_of_date: date | None = None
    method_fallback: bool = False  # 0002 3-3 (스키마 반영 전, #10)
    risk_basis: str = "SKU"  # 0002 3-8
    no_date_receipt_qty: int = 0  # 0002 3-8
    result: ProductResult

    @property
    def context(self) -> Context:
        return Context(
            as_of_date=self.as_of_date,
            method_fallback=self.method_fallback,
            risk_basis=self.risk_basis,
            no_date_receipt_qty=self.no_date_receipt_qty,
        )

    def scenario(self, t: ScenarioType):
        return next((s for s in self.result.scenarios if s.type == t), None)


_STATUS = {
    "NORMAL": ResultStatus.OK,
    "NO_RESTOCK_NEEDED": ResultStatus.NOT_NEEDED,
    "CANNOT_COMPUTE": ResultStatus.NOT_CALCULABLE,
}
_TYPE = {"s80": ScenarioType.P80, "s100": ScenarioType.P100, "s120": ScenarioType.P120, "custom": ScenarioType.CUSTOM}


def from_reference(d: dict, product_id: int = 0) -> EvalCase:
    """참조 구현 EngineResult(dict) → EvalCase. 필드 대응은 0002 · tests/fixtures/insight_prompts.md 이름표."""
    inputs, f, rec = d.get("inputs") or {}, d.get("forecast"), d.get("recommendation")
    before = d.get("stockout_risk_before_arrival")
    computed = bool(before and before.get("computed"))
    status = _STATUS[d["status"]]
    r = {"product_id": product_id, "status": status, "reason": d.get("reason")}
    if f is not None:
        r |= {
            "method": Method(f["method"]),
            "method_reason": f.get("method_reason"),
            "multiplier": f["multiplier"],
            "forecast_outbound_qty": f["cover_demand"],
        }
    if inputs:
        r |= {
            "horizon_days": inputs["cover_days"],
            "start_stock_qty": inputs["current_stock"],
            "planned_receipts_in_horizon": sum(p["qty"] for p in inputs.get("pending_orders", [])),
        }
    if rec is not None:
        r |= {"recommended_qty": rec["qty"], "required_qty_before_rounding": rec.get("raw_qty")}
    if status == ResultStatus.OK:
        r["risk_status"] = RiskStatus.OK if computed else RiskStatus.INSUFFICIENT_SAMPLE
        if before is not None:
            r |= {
                "pre_arrival_risk": before.get("risk") if computed else None,
                "risk_sample_size": before.get("sample_size"),
            }
    r["scenarios"] = [
        {
            "type": _TYPE[s["key"]],
            "qty": s["qty"],
            "is_duplicate": s.get("duplicate_of") is not None,
            "expected_depletion_on": s.get("depletion_date"),
            "expected_ending_stock": s["remaining_at_next_restock"],
            "unmet_demand_qty": s.get("unmet_demand", 0),
            "post_arrival_risk": (s["stockout_risk_after_arrival"].get("risk") if computed else None),
        }
        for s in d.get("scenarios", [])
    ]
    return EvalCase(
        id=d["calc_id"],
        name=(d.get("sku") or {}).get("name"),
        as_of_date=d.get("base_date"),
        method_fallback=bool(f and f.get("method_reason")),  # 참조 구현은 A 대신 B를 쓸 때만 method_reason이 있음
        risk_basis="POOLED" if before and before.get("pooled") else "SKU",
        no_date_receipt_qty=inputs.get("unreflected_pending_qty", 0),
        result=ProductResult.model_validate(r),
    )


def all_values(case: EvalCase) -> dict[str, str]:
    """Spring이 채울 값: #44 기준 구현의 6장 키 + LLM이 쓸 수 있는 나머지 5장 키(이름 · 소진일 · 미충족 · 차이)."""
    r = case.result
    v = placeholder_values(r, case.context)
    described = [s for s in r.scenarios if not s.is_duplicate]
    base = next((s for s in described if s.type == ScenarioType.P100), None)
    for s in described:
        x = s.type.value.lower()
        v[f"label_{x}"] = SCENARIO_NAMES[x]
        v[f"depletion_{x}"] = (
            _date(s.expected_depletion_on) if s.expected_depletion_on else "커버 기간 안에 소진되지 않음"
        )
        v[f"unmet_{x}"] = _int(s.unmet_demand_qty)
        if base is not None and s is not base:
            v[f"diff_{x}"] = _int(abs(s.qty - base.qty))
    return v


def _dir(a: float, b: float) -> str:
    a, b = _round(a), _round(b)
    return "높음" if a > b else "낮음" if a < b else "같음"


def to_input(case: EvalCase) -> InsightInput:
    """to_insight_input() 대신 (#10 "별도 Issue"). 값이 있는 숫자 키 + 화면에 보이는 값으로 비교한 방향(#4 문서 6-4)."""
    r = case.result
    keys = [k for k in all_values(case) if not PHRASE_RE.match(k)]
    base = case.scenario(ScenarioType.P100)
    dirs = {}
    for s in r.scenarios:
        if s.is_duplicate or s is base or base is None:
            continue
        x = s.type.value.lower()
        dirs[f"ending_{x}_vs_p100"] = _dir(s.expected_ending_stock, base.expected_ending_stock)
        if r.risk_status == RiskStatus.OK and s.post_arrival_risk is not None and base.post_arrival_risk is not None:
            dirs[f"risk_post_{x}_vs_p100"] = _dir(s.post_arrival_risk * 100, base.post_arrival_risk * 100)
    return InsightInput(
        sku_label="(이 상품)",
        recommended=ScenarioType.P100,
        placeholders=keys,
        directions=dirs,
        risk_status=r.risk_status,
    )


def verify_filled(filled: str, values: dict[str, str]) -> tuple[bool, str | None]:
    """참조 구현 validator.verify_filled: 값을 넣은 문장의 숫자가 모두 값 목록에서 온 것인지 (Spring 값 대조 대신)."""
    allowed = {t for x in values.values() for t in NUMBER_RE.findall(x)}
    bad = [t for t in NUMBER_RE.findall(filled) if t not in allowed]
    if bad:
        return False, f"값 목록에 없는 숫자 {bad[:5]}"
    if "{{" in filled or "}}" in filled:
        return False, "치환되지 않은 자리 표시자"
    return True, None
