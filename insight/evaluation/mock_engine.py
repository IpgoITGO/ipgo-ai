"""가짜 엔진 결과 생성기. 참조 구현(ai_insight v0.3) evaluation/mock_engine.py를 그대로 옮기고,
마지막에 참조 구현 EngineResult 형식(dict)을 0002 형식(EvalCase)으로 바꾼다(cases.from_reference).

실제 계산 엔진이 연결되기 전에 평가 도구 전체를 돌려 보기 위한 것이다.
DA-04 산식과 DA-06 날짜별 재고 규칙(입고 후 출고, 미충족 수요 비이월)은 지키지만 수요와 위험은 단순화했다.
본 실험에는 실제 엔진 결과를 쓴다.

확인 필요(LOW_GRADE) 결과: 참조 구현에는 정확도 등급이 없다. 비율은 eval.yaml mock.low_grade_rate(기본 0, #54)이고,
예상 출고량 범위를 만드는 규칙(DA-13)이 아직 없어 0보다 크게 두면 멈춘다.
"""

import math
import random
from datetime import date, timedelta

from .cases import EvalCase, from_reference

PRODUCTS = [
    "손선풍기 미니",
    "넥밴드 선풍기",
    "탁상 선풍기",
    "휴대용 보조배터리",
    "쿨링 타월",
    "텀블러 대용량",
    "아이스팩 세트",
    "차량용 방향제",
    "USB 가습기",
    "접이식 우산",
    "방수 파우치",
    "모기 기피 밴드",
    "캠핑 랜턴",
    "쿨매트 싱글",
    "자외선 차단 팔토시",
    "얼음 트레이",
]


def _ceil_unit(x: float, unit: int) -> int:
    return int(math.ceil(x / unit) * unit) if x > 0 else 0


def _simulate(daily, start, inbound_day, inbound_qty, pending, base):
    stock, unmet, stockout = start, 0, None
    for i, d in enumerate(daily, start=1):
        if i == inbound_day:
            stock += inbound_qty
        for day, q in pending:
            if i == day:
                stock += q
        if d > stock:
            unmet += d - stock
            stock = 0
        else:
            stock -= d
        if stock == 0 and stockout is None:
            stockout = base + timedelta(days=i)
    return stock, unmet, stockout


def _cmp(a, b):
    return "higher" if a > b else "lower" if a < b else "same"


def _risk(p, n, pooled=False):
    return {"computed": p is not None, "risk": p, "sample_size": n, "pooled": pooled}


def mock_result(rng: random.Random, idx: int, base: date = date(2026, 9, 15)) -> EvalCase:
    while True:
        mean = rng.uniform(4, 45)
        lead = rng.choice([3, 5, 5, 7])
        cycle = rng.choice([14, 21, 28])
        H = lead + cycle
        mult = rng.choice([1.0, 1.05, 1.10, 1.15])
        unit = rng.choice([1, 10, 10, 20, 50])
        daily = [max(0, round(rng.gauss(mean, mean * 0.35))) for _ in range(H)]
        demand = round(mean * H, 1)
        current = int(mean * rng.uniform(lead * 0.6, lead + 6))
        pending = []
        if rng.random() < 0.35:
            pending = [(rng.randint(1, H), _ceil_unit(mean * rng.uniform(3, 8), unit))]
        raw = demand * mult - current - sum(q for _, q in pending)
        rec = _ceil_unit(raw, unit)
        if rec > 0:
            break

    n = rng.randint(6, 60)
    small = n < 10
    pooled = (not small) and rng.random() < 0.2
    rb = None if small else round(min(0.95, max(0.01, 0.9 - 0.6 * current / max(1, mean * lead))), 2)

    scen, base_vals = [], None
    q100 = _ceil_unit(rec, unit)
    for key, label, p in (("s80", "80%", 0.8), ("s100", "100%", 1.0), ("s120", "120%", 1.2)):
        q = _ceil_unit(rec * p, unit)
        dup = (
            "s100" if key != "s100" and q == q100 else None
        )  # 샘플(engine_result_duplicate)과 같이 s100 이 아닌 쪽에 표시
        remain, unmet, so = _simulate(daily, current, lead, q, pending, base)
        ra = (
            None
            if small
            else round(min(0.95, max(0.01, 0.6 - 0.45 * remain / max(1, mean * 3) + (0.25 if unmet else 0))), 2)
        )
        scen.append(
            {
                "key": key,
                "label": label,
                "qty": q,
                "depletion_date": so,
                "remaining_at_next_restock": remain,
                "unmet_demand": unmet,
                "stockout_risk_after_arrival": _risk(ra, n, pooled),
                "duplicate_of": dup,
            }
        )
        if key == "s100":
            base_vals = (remain, ra)
    for s in scen:
        if s["key"] != "s100":
            ra = s["stockout_risk_after_arrival"]["risk"]
            s["vs_recommended"] = {
                "remaining": _cmp(s["remaining_at_next_restock"], base_vals[0]),
                "risk_after": None if (ra is None or base_vals[1] is None) else _cmp(ra, base_vals[1]),
            }

    return from_reference(
        {
            "calc_id": f"MOCK-{idx:03d}",
            "engine_version": "mock",
            "base_date": base,
            "sku": {"barcode": f"880000{idx:07d}", "name": PRODUCTS[idx % len(PRODUCTS)]},
            "status": "NORMAL",
            "inputs": {
                "lead_time_days": lead,
                "restock_cycle_days": cycle,
                "cover_days": H,
                "order_unit": unit,
                "current_stock": current,
                "pending_orders": [{"qty": q, "arrival_date": base + timedelta(days=d)} for d, q in pending],
            },
            "forecast": {
                "method": rng.choice(["A", "B"]),
                "cover_demand": demand,
                "multiplier": mult,
                "recent_28d_avg": round(mean, 1),
            },
            "recommendation": {"qty": rec, "raw_qty": round(raw, 1)},
            "stockout_risk_before_arrival": _risk(rb, n, pooled),
            "scenarios": scen,
        },
        product_id=idx + 1,
    )


def mock_bank(n: int, seed: int = 7, low_grade_rate: float = 0.0) -> list[EvalCase]:
    if low_grade_rate > 0:
        raise SystemExit(
            "mock.low_grade_rate > 0: LOW_GRADE 예상 출고량 범위 규칙(DA-13)이 없어 "
            "가짜 결과를 만들 수 없어요 (#54 확인 필요)"
        )
    rng = random.Random(seed)
    return [mock_result(rng, i) for i in range(n)]
