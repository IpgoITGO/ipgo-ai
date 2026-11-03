"""이해도 문항 자동 생성 — 정답은 계산 결과에서 바로 계산한다. 참조 구현 evaluation/quiz.py를 0002 필드로 옮겼다.

문항은 세 조건 모두 비교표만 보고도 풀 수 있게 만든다. 해설이 "더 빨리, 덜 틀리게" 돕는지만 본다.
  Q1 basis    : 권장량 계산에 쓴 커버 기간 예상 출고량 (근거 이해)
  Q2 diff     : 120%로 넣으면 100% 대비 다음 입고 시점 잔여 재고가 어떻게 되는지 (시나리오 비교)
  Q3 stockout : 다음 입고 시점 전에 재고가 바닥나는 시나리오 (위험 읽기)
  Q4 before   : 120%일 때 도착 전 품절 위험 (도착 전·후 위험 구분, 도착 전은 시나리오 공통)
2단계(사람 실험)에서 쓰지만 참조 구현 prepare가 실험용 문장과 함께 고정하므로 같이 옮겼다.
"""

import hashlib
import random
from dataclasses import asdict, dataclass

from schemas.base import RiskStatus, ScenarioType
from tests.insight_fallback import _int, _pct, _round

from .cases import EvalCase


@dataclass
class Question:
    id: str
    kind: str
    prompt: str
    options: list[dict]  # [{"id": "a", "text": "..."}]
    answer: str  # 정답 option id

    def public(self) -> dict:
        d = asdict(self)
        d.pop("answer")
        return d


def _q(n: float) -> str:
    return f"{_int(n)}개"


def _mk(
    qid: str, kind: str, prompt: str, correct: str, distractors: list[str], seed: str, shuffle: bool = True
) -> Question:
    texts = [correct] + [d for d in dict.fromkeys(distractors) if d != correct]
    texts = texts[:4]
    if shuffle:
        random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16)).shuffle(texts)
    opts = [{"id": "abcd"[i], "text": t} for i, t in enumerate(texts)]
    return Question(qid, kind, prompt, opts, next(o["id"] for o in opts if o["text"] == correct))


def make_questions(case: EvalCase) -> list[Question]:
    r = case.result
    s80, s100, s120 = (case.scenario(t) for t in (ScenarioType.P80, ScenarioType.P100, ScenarioType.P120))
    assert r.forecast_outbound_qty is not None and r.recommended_qty and s80 and s100 and s120, (
        "실험용 결과는 정상 상태와 80% · 100% · 120% 시나리오가 필요합니다"
    )
    cid, cur = case.id, r.start_stock_qty
    demand = _round(r.forecast_outbound_qty)
    qs: list[Question] = []

    qs.append(
        _mk(
            f"{cid}-q1",
            "basis",
            "권장 입고량을 계산할 때 쓴 커버 기간 예상 출고량은 몇 개인가요?",
            _q(demand),
            [
                _q(r.forecast_outbound_qty * r.multiplier),
                _q(r.recommended_qty),
                _q(cur),
                _q(demand + cur),
                _q(demand * 0.8),
            ],
            f"{cid}q1",
        )
    )

    e100, e120 = _round(s100.expected_ending_stock), _round(s120.expected_ending_stock)
    diff = e120 - e100
    correct = "변하지 않는다" if diff == 0 else f"{_q(abs(diff))} {'늘어난다' if diff > 0 else '줄어든다'}"
    qd = s120.qty - s100.qty
    distract = [
        f"{_q(qd)} 늘어난다",
        f"{_q(e120)} 늘어난다",
        f"{_q(abs(diff))} 줄어든다",
        "변하지 않는다",
        f"{_q(abs(diff) * 2 if diff else max(10, qd * 2))} 늘어난다",
        f"{_q(max(10, abs(qd) // 2))} 줄어든다",
        f"{_q(abs(qd) + 10)} 늘어난다",
    ]
    distract = [d for d in distract if not d.startswith("0개 ")]  # "0개 늘어난다"는 "변하지 않는다"와 같은 뜻
    qs.append(
        _mk(
            f"{cid}-q2",
            "diff",
            "120%로 넣으면 100%와 비교해 다음 입고 시점 잔여 재고는 어떻게 되나요?",
            correct,
            distract,
            f"{cid}q2",
        )
    )

    out = [lbl for lbl, s in (("80%", s80), ("100%", s100), ("120%", s120)) if _round(s.expected_ending_stock) == 0]
    choices = ["없음", "80%만", "80%, 100%", "80%, 100%, 120% 모두"]
    key = {0: "없음", 1: "80%만", 2: "80%, 100%", 3: "80%, 100%, 120% 모두"}[len(out)]
    if out and out[0] != "80%":  # 단조성이 깨진 특이 사례
        key = ", ".join(out)
        choices = [key] + choices[:3]
    q3 = Question(
        f"{cid}-q3",
        "stockout",
        "다음 입고 시점 전에 재고가 바닥나는(잔여 재고 0개) 시나리오는?",
        [{"id": "abcd"[i], "text": t} for i, t in enumerate(choices)],
        "",
    )
    q3.answer = next(o["id"] for o in q3.options if o["text"] == key)
    qs.append(q3)

    ra = {k: s.post_arrival_risk for k, s in (("s80", s80), ("s100", s100), ("s120", s120))}
    rb = r.pre_arrival_risk
    if r.risk_status == RiskStatus.OK and rb is not None and all(x is not None for x in ra.values()):
        qs.append(
            _mk(
                f"{cid}-q4",
                "before",
                "120%로 넣을 때 '도착 전' 품절 위험은 얼마인가요?",
                _pct(rb),
                [
                    _pct(ra["s120"]),
                    _pct(ra["s100"]),
                    _pct(round(1 - rb, 2)),
                    _pct(ra["s80"]),
                    _pct(min(1.0, rb * 1.2 + 0.05)),
                ],
                f"{cid}q4",
            )
        )
    return qs


def score(questions: list[Question], answers: dict[str, str]) -> tuple[int, int, dict[str, bool]]:
    per = {q.id: answers.get(q.id) == q.answer for q in questions}
    return sum(per.values()), len(questions), per
