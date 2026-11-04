"""시나리오 은행 만들기와 실험 자료 준비 (1단계 오프라인 자동 평가 포함). 참조 구현 evaluation/bank.py를 옮겼다.

make_bank : 계산 결과 → 실험용 시나리오를 골라 조건 수만큼 세트로 나눈다 (난이도 층화). 참조 구현과 같음.
prepare   : 결과마다 고정 문장 · AI 작성 문장을 만들고 이해도 문항과 함께 고정(freeze)한다.
            전체 결과로 오프라인 평가 보고서를 만든다.

참조 구현과 다른 곳 (#54): 문장을 만드는 곳이 참조 구현 InsightService 하나가 아니다.
  - 초안 · 검사: 서비스 함수 insight.service.generate_insight (/engine/insight와 같은 코드, 이름만 보냄)
  - 값 넣기 · 숫자 대조 · 기본 문장: Spring 대신 #44 기준 구현(tests/insight_fallback.py) + 참조 구현 verify_filled
  - 입력: 서비스와 같은 #59 to_insight_input() (cases.to_input). None(생성 보류)이면 LLM을 부르지 않는다.
  - LLM을 부르는 것은 정상(OK) 결과만 (#24, IpgoITGO/ipgo-server#25 답변 5).
    참조 구현은 입고 불필요 + 직접 입력도 불렀다.
"""

import json
import math
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from insight.llm import LLMClient, LLMResult
from insight.prompt import PROMPT_VERSION, build_prompt, draft_text
from insight.service import generate_insight
from insight.validator import check_draft
from schemas.base import ResultStatus, RiskStatus
from schemas.insight import Validation
from tests.insight_fallback import PLACEHOLDER_RE, build_fallback

from .cases import EvalCase, all_values, to_input, verify_filled
from .config import Config
from .quiz import make_questions
from .table import EXPERIMENT_KEYS

FALLBACK_SOURCE = "tests/insight_fallback.py (#44)"


class RecordingClient:
    """LLMClient를 감싸 마지막 호출 결과를 남긴다.

    generate_insight는 토큰 수 · 원문을 돌려주지 않기 때문이다 (서비스 코드는 그대로).
    """

    def __init__(self, inner: LLMClient):
        self.inner, self.model, self.last = inner, inner.model, None

    def complete(self, system: str, user: str, **kw) -> LLMResult:
        self.last = None
        self.last = self.inner.complete(system, user, **kw)
        return self.last


@dataclass
class GenResult:
    """참조 구현 InsightResult 중 1단계 보고서 · 판사에 쓰는 필드."""

    calc_id: str
    path: str  # llm(AI 작성 문장) / fixed(기본 문장) / none(문장 없음)
    text: str | None  # 표시할 문장
    template: str | None  # 자리 표시자 상태의 원문
    reason_code: str | None  # 기본 문장 · 문장 없음으로 간 사유
    reason_detail: str | None
    fixed_text: str | None
    fixed_template: str | None
    number_check_ok: bool
    called: bool  # LLM을 불렀는지 (참조 구현 attempts가 있는지)
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    versions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def generate(case: EvalCase, client: LLMClient) -> GenResult:
    r = case.effective_result
    values = all_values(case)
    fb = build_fallback(r, case.context)
    fixed_t, fixed_text = (" ".join(fb.template), fb.text) if fb else (None, None)
    versions = {"prompt": PROMPT_VERSION, "fallback": FALLBACK_SOURCE, "model": None}

    def done(path, text=None, template=None, reason=None, detail=None, called=False, last=None, resp=None):
        if path != "llm":
            path, text, template = ("fixed", fixed_text, fixed_t) if fixed_text else ("none", None, None)
            if path == "none" and reason is None:
                reason, detail = "template_error", "기본 문장 조각에 필요한 값이 없음 (#44 시나리오 13)"
        ok = verify_filled(text, values)[0] if text else True
        return GenResult(
            case.id,
            path,
            text,
            template,
            reason,
            detail,
            fixed_text,
            fixed_t,
            ok,
            called,
            model=last.model if last else None,
            latency_ms=resp.latency_ms if resp else None,
            input_tokens=last.input_tokens if last else 0,
            output_tokens=last.output_tokens if last else 0,
            cost_usd=last.cost_usd if last else None,
            versions=versions | {"model": last.model if last else None},
        )

    if r.status != ResultStatus.OK:
        return done("fixed", detail=f"상태 {r.status.value}: Spring이 FastAPI를 부르지 않음")
    inp = to_input(case)
    if inp is None:  # #59 생성 보류: insightInput이 null이면 FastAPI를 부르지 않음
        return done("fixed", detail="insightInput null (#59 생성 보류): LLM을 부르지 않음")
    rec = RecordingClient(client)
    resp = generate_insight(inp, rec)
    last = rec.last
    if resp.validation != Validation.PASSED:
        if last is None:  # 호출 오류 · 시간 초과
            return done("fixed", reason=resp.fail_reason.value, called=True, resp=resp)
        # 서비스는 사유 코드를 돌려주지 않으므로 같은 함수로 다시 판정한다 (LLM은 다시 부르지 않음)
        prompt = build_prompt(inp)
        text = None if last.truncated else draft_text(last.text)
        code = "truncated" if last.truncated else "format"
        if text is not None:
            code = check_draft(text, prompt.allowed, prompt.required).violations[0].code
        return done("fixed", reason=code, called=True, last=last, resp=resp)
    try:
        filled = PLACEHOLDER_RE.sub(lambda m: values[m[1]], resp.draft)
    except KeyError as e:
        return done("fixed", reason="insert_error", detail=f"키 {e} 에 값이 없음", called=True, last=last, resp=resp)
    ok, why = verify_filled(filled, values)
    if not ok:
        return done("fixed", reason="insert_error", detail=why, called=True, last=last, resp=resp)
    return done("llm", text=filled, template=resp.draft, called=True, last=last, resp=resp)


def for_experiment(c: EvalCase) -> EvalCase:
    """실험 화면은 80 · 100 · 120% 세 시나리오만 쓴다.

    직접 입력(custom)은 셀러 입력값이라 빼고 문장도 그 기준으로 만든다.
    """
    r = c.result.model_copy(update={"scenarios": [s for s in c.result.scenarios if s.type in EXPERIMENT_KEYS]})
    return c.model_copy(update={"result": r})


def experiment_candidate(c: EvalCase) -> tuple[bool, str]:
    r = c.result
    if r.status != ResultStatus.OK:
        return False, f"상태 {r.status.value}"
    if any(c.scenario(k) is None for k in EXPERIMENT_KEYS):
        return False, "P80 · P100 · P120 중 빠진 시나리오"
    if any(c.scenario(k).is_duplicate for k in EXPERIMENT_KEYS):
        return False, "올림 후 시나리오 수량 중복"
    if (
        r.risk_status != RiskStatus.OK
        or r.pre_arrival_risk is None
        or any(c.scenario(k).post_arrival_risk is None for k in EXPERIMENT_KEYS)
    ):
        return False, "품절 위험 미산출 (표본 부족)"
    return True, ""


def _difficulty(c: EvalCase) -> tuple:
    n_out = sum(1 for k in EXPERIMENT_KEYS if math.floor(c.scenario(k).expected_ending_stock) == 0)
    return (n_out, c.result.method.value, bool(c.result.planned_receipts_in_horizon))


def make_bank(cases: list[EvalCase], cfg: Config, source: str) -> dict:
    e = cfg.experiment
    ok, excluded = [], {}
    for c in cases:
        good, why = experiment_candidate(c)
        if good:
            ok.append(for_experiment(c))
        else:
            excluded[c.id] = why
    need = e.n_sets * e.scenarios_per_set + e.practice_trials
    if len(ok) < need:
        raise SystemExit(
            f"실험에 쓸 수 있는 시나리오가 {len(ok)}개로 부족합니다 (필요 {need}개). "
            f"scenarios_per_set 을 줄이거나 기준일을 여러 개로 늘려 결과를 더 만드세요. 제외 사유: "
            f"{dict(Counter(excluded.values()))}"
        )
    ok.sort(key=lambda c: (_difficulty(c), c.id))
    practice = [ok.pop(len(ok) // 2) for _ in range(e.practice_trials)]
    total, n = e.n_sets * e.scenarios_per_set, e.n_sets
    picks = [ok[round(j * (len(ok) - 1) / (total - 1))] for j in range(total)] if total > 1 else ok[:1]
    sets: dict[str, list[str]] = {str(i): [] for i in range(n)}
    for row in range(e.scenarios_per_set):  # 난이도 순 ㄹ자 배분 → 세트 간 난이도 균형
        chunk = picks[row * n : (row + 1) * n]
        if row % 2:
            chunk = chunk[::-1]
        for s_no, c in enumerate(chunk):
            sets[str(s_no)].append(c.id)
    used = {x.id for x in practice} | {x for v in sets.values() for x in v}
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "all_results": [
            c.model_dump(mode="json", by_alias=True) for c in cases
        ],  # 오프라인 평가 대상 (원래 결과 그대로)
        "results": [c.model_dump(mode="json", by_alias=True) for c in practice + ok if c.id in used],  # 실험 화면용
        "sets": sets,
        "practice": [x.id for x in practice],
        "excluded": excluded,
    }


def _p(values: list[float], q: float):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(q * (len(v) - 1))))]


def prepare(bank: dict, cfg: Config, client: LLMClient, call_log: Path, runs: int = 1) -> dict:
    call_log.parent.mkdir(parents=True, exist_ok=True)
    outs = []
    with call_log.open("a", encoding="utf-8") as log:
        # 1단계: 원래 계산 결과 전체(직접 입력 포함)로 서비스 경로 그대로 평가. LLM은 확률적이므로 runs 번 반복 가능.
        for d in bank["all_results"]:
            c = EvalCase.model_validate(d)
            for run in range(runs):
                o = generate(c, client)
                rec = o.to_dict() | {"run": run, "logged_at": datetime.now().isoformat(timespec="seconds")}
                log.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                outs.append(o)
        # 실험 화면용 문장: P80 · P100 · P120만 남긴 결과로 한 번 만들어 고정
        items, versions = {}, {}
        for d in bank["results"]:
            c = EvalCase.model_validate(d)
            o = generate(c, client)
            log.write(json.dumps(o.to_dict() | {"run": "experiment"}, ensure_ascii=False, default=str) + "\n")
            if o.versions.get("model") or not versions:
                versions = o.versions
            items[c.id] = {
                "fixed_text": o.fixed_text,
                "llm_text": o.text,
                # llm = AI 작성 문장, fixed = 검사 실패 등으로 기본 문장 대체(실제 서비스와 같은 동작)
                "llm_source": "llm" if o.path == "llm" else "fixed_fallback",
                "llm_reason_code": o.reason_code,
                "questions": [q.__dict__ for q in make_questions(c)],
            }
    report = offline_report(outs, cfg, runs)
    return {
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "versions": versions,
        "bank": bank,
        "items": items,
        "offline_report": report,
    }


def offline_report(outs: list[GenResult], cfg: Config, runs: int) -> dict:
    """1단계 자동 평가: 경로 비율 · 사유 코드 · 수치 대조 · 응답시간 · 토큰 · 비용 (참조 구현과 같은 집계)."""
    called = [o for o in outs if o.called]
    n = len(called)
    passed = sum(1 for o in called if o.path == "llm")
    pass_rate = passed / n if n else None
    mismatch = sorted({o.calc_id for o in outs if o.text and not o.number_check_ok})
    lat = [o.latency_ms for o in called if o.latency_ms is not None]
    costs = [o.cost_usd for o in called]
    gate = cfg.offline_gate
    gate_ok = (
        pass_rate is not None and pass_rate >= gate.min_validation_pass_rate and len(mismatch) <= gate.max_fill_mismatch
    )
    return {
        "runs_per_result": runs,
        "n_generations": len(outs),
        "n_llm_called": n,
        "n_skipped_fixed": sum(1 for o in outs if not o.called and o.path == "fixed"),
        "path_counts": dict(Counter(o.path for o in outs)),
        "validation_pass_rate": round(pass_rate, 3) if pass_rate is not None else None,
        "fallback_rate": round(1 - pass_rate, 3) if pass_rate is not None else None,
        "reason_codes": dict(Counter(o.reason_code for o in called if o.reason_code).most_common()),
        "reason_details_sample": [o.reason_detail for o in called if o.reason_detail][:10],
        "final_text_number_mismatch": mismatch,
        "latency_ms": {"p50": _p(lat, 0.5), "p95": _p(lat, 0.95), "mean": round(statistics.mean(lat)) if lat else None},
        "tokens": {
            "input_total": sum(o.input_tokens for o in called),
            "output_total": sum(o.output_tokens for o in called),
        },
        "cost_usd": (
            {"total": round(sum(costs), 6), "per_call_mean": round(sum(costs) / n, 6)}
            if n and all(c is not None for c in costs)
            else None
        ),
        "gate": {
            "passed": bool(gate_ok),
            "min_validation_pass_rate": gate.min_validation_pass_rate,
            "max_fill_mismatch": gate.max_fill_mismatch,
        },
    }


def save_json(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
