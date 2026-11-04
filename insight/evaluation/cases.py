"""평가 사례: 0002 ProductResult + 계산 결과 밖에서 오는 값(Context). 참조 구현 EngineResult를 대신한다.

- 참조 구현(ai_insight v0.3)의 계산 결과 형식(EngineResult)을 0002 이름으로 옮기는 변환(from_reference)을 둔다.
  참조 구현 samples/ · mock_engine 결과를 그대로 쓰기 위해서다.
- 인사이트 입력(to_input)은 서비스와 같은 #59 engine.insight_facts.to_insight_input()을 쓴다. EvalCase가 따로 가진
  계산 밖 값(method_fallback · risk_basis · no_date_receipt_qty)은 #44 Context에도 쓰여 그대로 두고, 변환 전에
  #58 ProductResult 필드에 넣는다.
- 값 넣기 · 숫자 대조 · 기본 문장은 Spring 대신 #44 기준 구현(tests/insight_fallback.py)을 쓴다.
"""

import re
from datetime import date

from pydantic import BaseModel

from engine.insight_facts import to_insight_input
from schemas.base import Method, ResultStatus, RiskBasis, RiskStatus, ScenarioType
from schemas.engine import ProductResult
from schemas.insight import InsightInput
from tests.insight_fallback import Context, _date, _demand, _int, placeholder_values

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
    method_fallback: bool = False  # 0002 3-3. #44 Context용, 입력 변환 때 ProductResult.method_fallback(#58)으로
    risk_basis: str = "SKU"  # 0002 3-8
    no_date_receipt_qty: int = 0  # 0002 3-8
    result: ProductResult

    @property
    def effective_result(self) -> ProductResult:
        """실제 결과 메타데이터를 우선하고 이전 형식의 계산 밖 값으로만 보완한다."""
        r = self.result
        return r.model_copy(
            update={
                "method_fallback": self.method_fallback if r.method_fallback is None else r.method_fallback,
                "risk_basis": RiskBasis(self.risk_basis) if r.risk_basis is None else r.risk_basis,
                "no_date_receipt_qty": r.no_date_receipt_qty or self.no_date_receipt_qty,
            }
        )

    @property
    def context(self) -> Context:
        r = self.effective_result
        return Context(
            as_of_date=self.as_of_date,
            method_fallback=bool(r.method_fallback),
            risk_basis=RiskBasis(r.risk_basis).value,
            no_date_receipt_qty=r.no_date_receipt_qty,
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
    r = case.effective_result
    v = placeholder_values(r, case.context)
    described = [s for s in r.scenarios if not s.is_duplicate]
    base = next((s for s in described if s.type == ScenarioType.P100), None)
    for s in described:
        x = s.type.value.lower()
        v[f"label_{x}"] = SCENARIO_NAMES[x]
        v[f"depletion_{x}"] = (
            _date(s.expected_depletion_on) if s.expected_depletion_on else "커버 기간 안에 소진되지 않음"
        )
        v[f"unmet_{x}"] = _demand(s.unmet_demand_qty)  # 서버 DisplayQuantities.demand (미충족 수요 올림)
        if base is not None and s is not base:
            v[f"diff_{x}"] = _int(abs(s.qty - base.qty))
    return v


def to_input(case: EvalCase) -> InsightInput | None:
    """서비스와 같은 입력: #59 to_insight_input(). result에 값이 없으면 EvalCase의 계산 밖 값을 #58 필드로 넣는다.

    None은 #59의 생성 보류(필수 사실 없음)다. prepare는 이때 LLM을 부르지 않는다 (bank.generate).
    """
    return to_insight_input(case.effective_result, "(이 상품)", as_of_date=case.as_of_date)


def verify_filled(filled: str, values: dict[str, str]) -> tuple[bool, str | None]:
    """참조 구현 validator.verify_filled: 값을 넣은 문장의 숫자가 모두 값 목록에서 온 것인지 (Spring 값 대조 대신)."""
    allowed = {t for x in values.values() for t in NUMBER_RE.findall(x)}
    bad = [t for t in NUMBER_RE.findall(filled) if t not in allowed]
    if bad:
        return False, f"값 목록에 없는 숫자 {bad[:5]}"
    if "{{" in filled or "}}" in filled:
        return False, "치환되지 않은 자리 표시자"
    return True, None
