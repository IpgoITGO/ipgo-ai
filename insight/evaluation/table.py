"""실험 화면의 비교표 (FE-04 계산 근거 + FE-06 시나리오 비교표). 참조 구현 evaluation/table.py를 0002 필드로 옮겼다.

세 조건(비교표만 · 고정 문장 · AI 작성 문장) 모두 이 표를 똑같이 보여 준다. 달라지는 것은 표 아래 해설뿐이다.
숫자 형식은 #44 기준 구현(tests/insight_fallback.py, #4 문서 6-4)과 같게 해서 문장 속 숫자와 같은 모양이 되게 한다
(재고 내림 · 예상 출고량 올림: 서버 DisplayQuantities, f80e67b).
참조 구현의 "리드타임" · "입고 단위" 행은 0002 ProductResult에 없는 값이라 뺐다.
"""

from schemas.base import RiskStatus, ScenarioType
from tests.insight_fallback import _date, _demand, _int, _pct, _stock

from .cases import EXPERIMENT_TYPES, EvalCase

COLUMNS = ["시나리오", "입고 수량", "예상 소진일", "다음 입고 시점 잔여 재고", "도착 후 품절 위험"]
EXPERIMENT_KEYS = EXPERIMENT_TYPES
LABELS = {
    ScenarioType.P80: "80%",
    ScenarioType.P100: "100%",
    ScenarioType.P120: "120%",
    ScenarioType.CUSTOM: "직접 입력",
}


def _risk(case: EvalCase, risk: float | None, with_n: bool = False) -> str:
    r = case.result
    if r.risk_status != RiskStatus.OK or risk is None:
        return "미산출 (표본 부족)" if with_n else "미산출"
    s = _pct(risk)
    if with_n:
        s += f" (표본 {r.risk_sample_size}건{', 전체 상품 기준' if case.risk_basis == 'POOLED' else ''})"
    return s


def render_table(case: EvalCase) -> dict:
    r = case.result
    facts: list[tuple[str, str]] = [("상품", case.name or case.id)]
    if case.as_of_date:
        facts.append(("기준일", _date(case.as_of_date)))
    if r.method is not None and r.recommended_qty is not None:
        facts += [
            ("권장 입고량", f"{_int(r.recommended_qty)}개"),
            ("적용 산식", f"계산식 {r.method.value}"),
            ("배수", _pct(r.multiplier)),
            ("커버 기간", f"{_int(r.horizon_days)}일"),
            ("커버 기간 예상 출고", f"{_demand(r.forecast_outbound_qty)}개"),
            ("현재 재고", f"{_stock(r.start_stock_qty)}개"),
            ("커버 기간 내 도착 예정 물량", f"{_int(r.planned_receipts_in_horizon)}개"),
        ]
    facts.append(("도착 전 품절 위험 (모든 시나리오 공통)", _risk(case, r.pre_arrival_risk, with_n=True)))
    rows = []
    for s in r.scenarios:
        note = " (= 100%)" if s.is_duplicate else ""
        rows.append(
            [
                f"{LABELS[s.type]}{note}",
                f"{_int(s.qty)}개",
                _date(s.expected_depletion_on) if s.expected_depletion_on else "",
                f"{_stock(s.expected_ending_stock)}개",
                _risk(case, s.post_arrival_risk),
            ]
        )
    return {"facts": facts, "columns": COLUMNS, "rows": rows}
