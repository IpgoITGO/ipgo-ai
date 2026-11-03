"""LLM 판사 — AI 인사이트 문장의 '의미'를 채점하는 오프라인 평가 (관문 아님, 참고 지표).
참조 구현(ai_insight v0.3) evaluation/judge.py를 옮겼다. 기준표 · 판사 prompt · 집계는 그대로이고,
계산 결과 형식(EvalCase, 0002)과 자리 표시자 이름(tests/fixtures/insight_prompts.md 이름표)만 바꿨다.
판사 호출기는 judge_client.py (#54: gpt-5.6-sol은 max_completion_tokens, temperature 안 보냄).

validator.py 는 숫자·방향 어휘·금지 어휘처럼 글자로 잡을 수 있는 것만 본다. 근거를 거꾸로 설명하거나
시나리오 비교를 뒤집어 말하는 문장은 통과한다. 이 모듈은 그 빈자리를 두 겹으로 메운다.

1) 포함 여부 검사 (코드, LLM 없음)
   문장 템플릿에 쓰인 자리 표시자로 "다뤄야 할 내용을 다뤘는가"를 본다. 판사보다 정확하고 비용이 없다.
   C1 권장량 · C2 줄일 때(80%) · C3 늘릴 때(120%) · C4 근거 수치 · C5 도착 전 위험
   (AI-01 "권장량의 근거와 입고량을 늘리거나 줄일 때의 변화")

2) LLM 판사 (의미 판정)
   셀러가 보는 계산 결과 표와 값이 채워진 최종 문장을 주고 기준마다 pass / fail / na 를 받는다.
   J1 근거 설명 · J2 시나리오 비교 해석 · J3 없는 사실 · J4 결정 강요·권장량 변경 · J5 추정치 표현
   - 판정 전에 근거 인용(evidence)과 이유(reason)를 먼저 쓰게 한다.
   - 같은 문장을 runs 번(기본 2) 판정하고, 두 번째는 기준 순서를 거꾸로 준다. 모두 같을 때만 확정, 아니면 disagree.
   - 판사 모델은 생성 모델과 다르게 둔다(자기 선호 편향). 같으면 보고서에 경고를 남긴다.

판사 결과는 사람 채점과의 일치율(judge-agree)을 먼저 확인한 뒤에만 근거로 쓴다.
판사 설정은 config/eval.yaml 의 judge 절이며, 사전 등록(lock)한 채택 규칙·관문에는 포함되지 않는다.
"""

import csv
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from insight.llm import LLMClient, LLMError, LLMResult
from schemas.base import ResultStatus, RiskStatus, ScenarioType
from tests.insight_fallback import PLACEHOLDER_RE

from .cases import EvalCase
from .table import render_table

JUDGE_PROMPT_VERSION = "judge-2026.10.26-v0.1"
VERDICTS = ("pass", "fail", "na")


# --- 기준표 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Criterion:
    id: str
    name: str
    question: str
    fail_examples: str
    allow_na: bool
    source: str


CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        "J1",
        "근거 설명",
        "문장이 권장량의 근거(적용 산식, 커버 기간 예상 출고량, 여유 배수, 현재 재고, 도착 예정 물량)를 언급했다면 "
        "그 관계가 [계산 구조]와 맞는가? 근거를 하나도 언급하지 않았으면 na.",
        "도착 예정 물량을 '더했다'고 쓰기, 배수를 재고에 곱했다고 쓰기, 예상 출고량과 권장량을 같은 것처럼 쓰기",
        True,
        "AI-01 · 기획서 '입고 수량 계산 방식'",
    ),
    Criterion(
        "J2",
        "시나리오 비교 해석",
        "시나리오끼리 비교한 내용(어느 시나리오가 남는 재고가 많고 적은지, 품절 위험이 높고 낮은지, 그 수치가 어느 "
        "시나리오의 것인지)이 표의 값과 맞는가? 시나리오를 비교하지 않았으면 na.",
        "80% 시나리오의 위험을 120%의 것처럼 쓰기, 연결어 때문에 '줄이면 남는 재고가 늘어난다'처럼 방향이 뒤집힌 문장",
        True,
        "AI-01",
    ),
    Criterion(
        "J3",
        "없는 사실 지어내기",
        "표와 [계산 구조]에 없는 원인·사실·전망(프로모션, 계절, 수요 급증, 경쟁 상품, 가격, 광고 등)을 "
        "덧붙이지 않았는가?",
        "'성수기라 수요가 늘어', '행사 영향으로', '앞으로 판매가 줄 것으로 보여'",
        False,
        "AI-01 · 기획서 'AI는 계산 결과를 읽기 쉬운 문장으로 설명한다'",
    ),
    Criterion(
        "J4",
        "결정 강요·권장량 변경",
        "특정 수량을 넣으라고 권하거나, 결정을 대신 내리거나, 권장량·계산 방식·배수를 바꾸자는 뜻의 표현이 없는가? "
        "금지 어휘를 피해 돌려 말한 경우도 포함한다.",
        "'120% 쪽이 낫습니다', '이 정도면 걱정 없습니다', '권장량보다 조금 더 넣는 편이 좋겠습니다'",
        False,
        "AI-01 완료조건 · 기획서 '최종 입고 여부와 수량은 셀러가 결정한다'",
    ),
    Criterion(
        "J5",
        "추정치 표현",
        "품절 위험을 언급했다면 확정된 사실이 아니라 과거 오차 기준 추정치로 표현했는가? "
        "품절 위험을 언급하지 않았으면 na.",
        "'품절됩니다', '품절이 나지 않습니다', '위험이 없습니다'",
        True,
        "서비스 플로우 6장 '추정치 표기'",
    ),
)
CRITERIA_BY_ID = {c.id: c for c in CRITERIA}


@dataclass(frozen=True)
class CoverageCheck:
    id: str
    name: str
    description: str


COVERAGE: tuple[CoverageCheck, ...] = (
    CoverageCheck("C1", "권장량", "권장 입고량({{qty_p100}})을 말했는가"),
    CoverageCheck("C2", "줄일 때", "80% 시나리오를 다뤘는가 (설명 대상일 때)"),
    CoverageCheck("C3", "늘릴 때", "120% 시나리오를 다뤘는가 (설명 대상일 때)"),
    CoverageCheck("C4", "근거 수치", "근거 수치 자리 표시자(judge.basis_keys) 중 하나 이상을 썼는가"),
    CoverageCheck("C5", "도착 전 위험", "도착 전 품절 위험({{risk_pre}})을 말했는가 (산출됐을 때)"),
)


# --- 포함 여부 검사 (코드) --------------------------------------------------------------------


def _uses_scenario(keys: set[str], k: str) -> bool:
    return any(x.endswith(f"_{k}") or f"_{k}_" in x for x in keys)


def coverage_checks(template: str | None, case: EvalCase, basis_keys: list[str]) -> dict[str, bool | None]:
    """True=다룸, False=빠짐, None=해당 없음. 템플릿이 없으면(기본 문장 등) 전부 None."""
    out: dict[str, bool | None] = {c.id: None for c in COVERAGE}
    r = case.result
    if not template or r.status != ResultStatus.OK:
        return out
    used = set(PLACEHOLDER_RE.findall(template))
    described = {s.type for s in r.scenarios if not s.is_duplicate}
    out["C1"] = "qty_p100" in used
    if ScenarioType.P80 in described:
        out["C2"] = _uses_scenario(used, "p80")
    if ScenarioType.P120 in described:
        out["C3"] = _uses_scenario(used, "p120")
    out["C4"] = any(k in used for k in basis_keys)
    if r.risk_status == RiskStatus.OK and r.pre_arrival_risk is not None:  # 참조 구현: 카탈로그에 risk_before가 있을 때
        out["C5"] = "risk_pre" in used
    return out


# --- 판사 프롬프트 ----------------------------------------------------------------------------

JUDGE_SYSTEM = "\n".join([
    "[역할]",
    "당신은 입고 예측 서비스의 'AI 인사이트' 문장을 채점하는 검토자입니다. 셀러가 보는 계산 결과 표와 그 아래 "
    "붙는 설명 문장을 받습니다. 문장이 표와 맞는지, 지켜야 할 원칙을 지켰는지 기준별로 판정합니다. 문장을 고쳐 "
    "쓰거나 점수를 매기지 말고, 기준마다 pass / fail / na 중 하나만 고릅니다.",
    "",
    "[계산 구조]",
    "- 권장 입고량 = max(0, 커버 기간 예상 출고량 × 여유 배수 − 현재 재고 − 커버 기간 안에 도착하는 기존 발주 "
    "물량)을 입고 단위로 올린 값입니다.",
    "- 80%·120% 시나리오는 권장량의 80%·120%를 입고 단위로 올린 수량입니다. 입고량을 늘리면 남는 재고는 늘거나 "
    "같고, 도착 후 품절 위험은 낮아지거나 같습니다.",
    "- '도착 전 품절 위험'은 새 물량이 도착하기 전까지의 위험으로 모든 시나리오에 공통입니다. '도착 후 품절 "
    "위험'은 시나리오마다 다릅니다.",
    "- 품절 위험은 과거 예측 오차로 만든 추정치입니다.",
    "- 표에 없는 정보(판매 원인, 계절, 행사, 가격 등)는 계산에 쓰이지 않았습니다.",
    "",
    "[판정 방법]",
    "- 기준마다 먼저 evidence에 판정 근거가 되는 문장 부분을 그대로 인용하고(없으면 빈 문자열), reason에 표와 "
    "비교한 이유를 한두 문장으로 쓴 뒤, 마지막에 verdict를 정합니다.",
    "- 판정할 대상이 문장에 없으면 na입니다. na가 허용되지 않은 기준은 반드시 pass 또는 fail입니다.",
    "- 표의 값과 다르거나, 뜻이 애매해 셀러가 잘못 읽을 수 있으면 fail로 두고 reason에 그 점을 적습니다.",
    "- 문장이 짧거나 일부 시나리오를 다루지 않은 것 자체는 감점하지 않습니다(그건 별도로 검사합니다). 쓴 내용이 "
    "맞는지만 봅니다.",
    "",
    "[출력 형식]",
    "JSON 객체 하나만 출력합니다:",
    "{\"criteria\": [{\"id\": \"J1\", \"evidence\": \"...\", \"reason\": \"...\", \"verdict\": \"pass\"}, ...]}",
    "아래 [기준]의 모든 id를 빠짐없이 넣습니다.",
])  # 참조 구현 judge.py와 같은 글. 줄 길이(ruff E501) 때문에 줄 목록으로만 바꿈


def _criteria_block(order: list[Criterion]) -> str:
    lines = ["[기준]"]
    for c in order:
        na = "na 허용" if c.allow_na else "na 불가"
        lines.append(f"- {c.id} {c.name} ({na}): {c.question} 실패 예: {c.fail_examples}")
    return "\n".join(lines)


def build_judge_prompt(case: EvalCase, text: str, reverse: bool = False) -> tuple[str, str]:
    t = render_table(case)
    order = list(CRITERIA)[::-1] if reverse else list(CRITERIA)
    lines = ["[계산 결과 표 — 셀러가 보는 내용]"]
    lines += [f"{k}: {v}" for k, v in t["facts"]]
    lines.append(" | ".join(t["columns"]))
    lines += [" | ".join(row) for row in t["rows"]]
    lines += [
        "",
        "[설명 문장 — 채점 대상]",
        text,
        "",
        _criteria_block(order),
        "",
        "위 [판정 방법]대로 모든 기준을 판정해 JSON으로만 답하세요.",
    ]
    return JUDGE_SYSTEM, "\n".join(lines)


# --- 판정 실행 --------------------------------------------------------------------------------


@dataclass
class RunVerdicts:
    run: int
    ok: bool
    verdicts: dict[str, dict] = field(default_factory=dict)  # id -> {verdict, evidence, reason}
    error: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    raw: str = ""


def parse_judge(raw: str) -> tuple[dict[str, dict], str | None]:
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return {}, "JSON 아님"
        try:
            data = json.loads(m.group(0))
        except Exception:
            return {}, "JSON 아님"
    items = data.get("criteria") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}, "criteria 목록 없음"
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        cid, v = it.get("id"), str(it.get("verdict", "")).strip().lower()
        if cid not in CRITERIA_BY_ID or v not in VERDICTS:
            continue
        if v == "na" and not CRITERIA_BY_ID[cid].allow_na:
            continue  # na 불가 기준의 na 는 무효 → 빠진 것으로 처리
        out[cid] = {
            "verdict": v,
            "evidence": str(it.get("evidence", ""))[:500],
            "reason": str(it.get("reason", ""))[:800],
        }
    missing = [c.id for c in CRITERIA if c.id not in out]
    return out, (f"빠졌거나 무효인 기준: {', '.join(missing)}" if missing else None)


def judge_once(client: LLMClient, case: EvalCase, text: str, run: int, *, max_tokens: int) -> RunVerdicts:
    system, user = build_judge_prompt(case, text, reverse=bool(run % 2))
    try:
        resp: LLMResult = client.complete(system, user, max_tokens=max_tokens)
    except LLMError as e:
        return RunVerdicts(run, False, error=f"{type(e).__name__}: {e}")
    if resp.truncated:  # max_completion_tokens에는 추론 토큰도 들어간다 (#54)
        return RunVerdicts(
            run,
            False,
            error="출력 잘림 (max_tokens)",
            model=resp.model,
            latency_ms=resp.latency_ms,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            raw=resp.text,
        )
    verdicts, err = parse_judge(resp.text)
    return RunVerdicts(
        run, not err, verdicts, err, resp.model, resp.latency_ms, resp.input_tokens, resp.output_tokens, resp.text
    )


def combine(runs: list[RunVerdicts]) -> dict[str, dict]:
    """기준별 최종 판정. 유효한 판정이 모두 같으면 확정, 다르면 disagree, 하나도 없으면 error."""
    final: dict[str, dict] = {}
    for c in CRITERIA:
        got = [rv.verdicts[c.id] for rv in runs if c.id in rv.verdicts]
        if not got:
            final[c.id] = {"verdict": "error", "evidence": "", "reason": "유효한 판정 없음"}
            continue
        vs = {g["verdict"] for g in got}
        if len(vs) == 1 and len(got) == len(runs):
            final[c.id] = got[0] | {"verdict": got[0]["verdict"]}
        elif len(vs) == 1:  # 일부 실행만 유효 — 한 번만 본 판정은 확정하지 않는다
            final[c.id] = got[0] | {
                "verdict": "disagree",
                "reason": f"유효한 판정 {len(got)}/{len(runs)}회뿐: {got[0]['verdict']}",
            }
        else:
            final[c.id] = {
                "verdict": "disagree",
                "evidence": " / ".join(g["evidence"] for g in got),
                "reason": " / ".join(f"{g['verdict']}: {g['reason']}" for g in got),
            }
    return final


# --- 대상 고르기 ------------------------------------------------------------------------------


@dataclass
class Target:
    scope: str  # experiment | offline
    calc_id: str
    run: str
    text: str
    template: str | None
    engine: EvalCase


def _same_versions(a: dict, b: dict) -> bool:
    return all(
        a.get(k) == b.get(k) for k in ("prompt", "fallback", "model")
    )  # 참조 구현: prompt · rules · template · model


def select_targets(
    prepared: dict, call_log: Path, scope: str = "experiment", all_versions: bool = False
) -> list[Target]:
    """호출 로그에서 path=llm 인 문장을 고른다. 기본은 prepared 의 평가 대상 버전과 같은 기록만.
    experiment: 실험 화면에 고정된 문장(run="experiment"), offline: 1단계 오프라인 반복 생성, all: 둘 다.
    같은 calc_id·run 기록이 여러 번 있으면 마지막 것을 쓴다."""
    bank = prepared["bank"]
    exp = {d["id"]: d for d in bank["results"]}
    alld = {d["id"]: d for d in bank["all_results"]}
    want = prepared.get("versions") or {}
    picked: dict[tuple, Target] = {}
    for line in call_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("path") != "llm" or not rec.get("text"):
            continue
        if not all_versions and not _same_versions(rec.get("versions") or {}, want):
            continue
        is_exp = str(rec.get("run")) == "experiment"
        s = "experiment" if is_exp else "offline"
        if scope != "all" and s != scope:
            continue
        src = exp if is_exp else alld
        if rec["calc_id"] not in src:
            continue
        picked[(s, rec["calc_id"], str(rec.get("run")))] = Target(
            s,
            rec["calc_id"],
            str(rec.get("run")),
            rec["text"],
            rec.get("template"),
            EvalCase.model_validate(src[rec["calc_id"]]),
        )
    return list(picked.values())


# --- 실행·집계 --------------------------------------------------------------------------------


def _judge_cost(i: int, o: int) -> float | None:
    pin, pout = os.getenv("JUDGE_PRICE_IN_PER_1M"), os.getenv("JUDGE_PRICE_OUT_PER_1M")
    if not pin or not pout:
        return None
    return round(i * float(pin) / 1e6 + o * float(pout) / 1e6, 6)


def run_judge(
    targets: list[Target],
    client: LLMClient,
    *,
    runs: int = 2,
    max_tokens: int = 1200,
    basis_keys: list[str] | None = None,
    log_path: Path | None = None,
) -> list[dict]:
    basis_keys = basis_keys or ["forecast_outbound", "multiplier", "start_stock", "planned_receipts", "horizon_days"]
    rows = []
    log = log_path.open("a", encoding="utf-8") if log_path else None
    try:
        for t in targets:
            rv = [judge_once(client, t.engine, t.text, k, max_tokens=max_tokens) for k in range(runs)]
            row = {
                "scope": t.scope,
                "calc_id": t.calc_id,
                "run": t.run,
                "text": t.text,
                "coverage": coverage_checks(t.template, t.engine, basis_keys),
                "judge": combine(rv),
                "runs": [asdict(x) for x in rv],
            }
            rows.append(row)
            if log:
                log.write(
                    json.dumps(
                        row | {"judged_at": datetime.now().isoformat(timespec="seconds")},
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
    finally:
        if log:
            log.close()
    return rows


def _rate(p: int, n: int) -> float | None:
    return round(p / n, 3) if n else None


def judge_report(
    rows: list[dict],
    *,
    judge_model: str | None,
    generator_versions: dict,
    runs: int,
    client_kind: str,
    advisory_min_pass_rate: float = 0.9,
) -> dict:
    cov = {}
    for c in COVERAGE:
        vals = [(r["calc_id"], r["coverage"][c.id]) for r in rows if r["coverage"][c.id] is not None]
        ok = sum(1 for _, v in vals if v)
        cov[c.id] = {
            "name": c.name,
            "description": c.description,
            "applicable": len(vals),
            "covered": ok,
            "rate": _rate(ok, len(vals)),
            "missing_calc_ids": sorted({cid for cid, v in vals if not v}),
        }
    crit = {}
    for c in CRITERIA:
        cnt = Counter(r["judge"][c.id]["verdict"] for r in rows)
        decided = cnt["pass"] + cnt["fail"]
        fails = [
            {
                "calc_id": r["calc_id"],
                "scope": r["scope"],
                "evidence": r["judge"][c.id]["evidence"],
                "reason": r["judge"][c.id]["reason"],
            }
            for r in rows
            if r["judge"][c.id]["verdict"] == "fail"
        ]
        crit[c.id] = {
            "name": c.name,
            "source": c.source,
            "pass": cnt["pass"],
            "fail": cnt["fail"],
            "na": cnt["na"],
            "disagree": cnt["disagree"],
            "error": cnt["error"],
            "pass_rate": _rate(cnt["pass"], decided),
            "below_advisory": (decided > 0 and cnt["pass"] / decided < advisory_min_pass_rate),
            "fail_examples": fails[:5],
        }
    all_runs = [x for r in rows for x in r["runs"]]
    lat = [x["latency_ms"] for x in all_runs if x.get("latency_ms") is not None]
    ti, to = sum(x["input_tokens"] for x in all_runs), sum(x["output_tokens"] for x in all_runs)

    def clean(r):
        cv = all(v is not False for v in r["coverage"].values())
        jv = all(v["verdict"] in ("pass", "na") for v in r["judge"].values())
        return cv and jv

    notes = []
    gen_model = (generator_versions or {}).get("model") or ""
    if judge_model and gen_model and judge_model.split("-20")[0] == gen_model.split("-20")[0]:
        notes.append(
            f"판사 모델({judge_model})이 생성 모델({gen_model})과 같은 계열입니다. "
            "자기 선호 편향이 있을 수 있어 다른 모델을 권합니다."
        )
    if client_kind == "stub":
        notes.append(
            "stub 판사의 판정은 파이프라인 점검용 규칙 판정입니다. 판정 근거로 쓰지 마세요. "
            "포함 여부 검사(C1~C5)는 실제 값입니다."
        )
    if any(x["error"] for x in all_runs):
        notes.append(
            f"판사 호출·형식 오류 {sum(1 for x in all_runs if x['error'])}회 — judge_calls.jsonl 의 runs[].error 확인"
        )
    notes.append(
        "이 보고서는 관문이 아닌 참고 지표입니다. 사람 채점과의 일치율(judge-agree)을 확인한 기준만 근거로 쓰세요."
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "judge": {
            "prompt_version": JUDGE_PROMPT_VERSION,
            "model": judge_model,
            "client": client_kind,
            "runs": runs,
            "criteria": [asdict(c) for c in CRITERIA],
        },
        "evaluated_versions": generator_versions,
        "n_texts": len(rows),
        "by_scope": dict(Counter(r["scope"] for r in rows)),
        "coverage": cov,
        "criteria": crit,
        "advisory_min_pass_rate": advisory_min_pass_rate,
        "texts_all_clear": sum(1 for r in rows if clean(r)),
        "latency_ms": {"p50": round(statistics.median(lat)) if lat else None, "max": max(lat) if lat else None},
        "tokens": {"input_total": ti, "output_total": to},
        "cost_usd": _judge_cost(ti, to),
        "notes": notes,
    }


# --- 사람 채점과의 일치율 ---------------------------------------------------------------------

REVIEW_FIELDS = [
    "scope",
    "calc_id",
    "run",
    "criterion",
    "criterion_name",
    "text",
    "judge_verdict",
    "judge_evidence",
    "judge_reason",
    "human_verdict",
    "human_note",
]


def write_review_csv(rows: list[dict], path: Path) -> None:
    """사람이 채점할 표. human_verdict 칸에 pass / fail / na 를 적어 judge-agree 로 넘긴다.
    판사 판정을 보고 따라 적지 않도록, 가능하면 judge_* 열을 숨기고 채점한 뒤 비교한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        w.writeheader()
        for r in rows:
            for c in CRITERIA:
                j = r["judge"][c.id]
                w.writerow(
                    {
                        "scope": r["scope"],
                        "calc_id": r["calc_id"],
                        "run": r["run"],
                        "criterion": c.id,
                        "criterion_name": c.name,
                        "text": r["text"],
                        "judge_verdict": j["verdict"],
                        "judge_evidence": j["evidence"],
                        "judge_reason": j["reason"],
                        "human_verdict": "",
                        "human_note": "",
                    }
                )


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    cats = sorted({x for p in pairs for x in p})
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in cats) / (n * n)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return round((po - pe) / (1 - pe), 3)


def agreement(csv_path: Path) -> dict:
    """판사(확정 판정만)와 사람의 기준별 일치율·카파. disagree·error·빈칸은 제외하고 그 수를 따로 센다."""
    by: dict[str, list[tuple[str, str]]] = defaultdict(list)
    skipped: Counter = Counter()
    with csv_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            h, j = row.get("human_verdict", "").strip().lower(), row.get("judge_verdict", "").strip().lower()
            if h not in VERDICTS:
                skipped["사람 채점 없음"] += 1
                continue
            if j not in VERDICTS:
                skipped[f"판사 {j or '빈칸'}"] += 1
                continue
            by[row["criterion"]].append((h, j))
    out = {}
    for c in CRITERIA:
        pairs = by.get(c.id, [])
        out[c.id] = {
            "name": c.name,
            "n": len(pairs),
            "agreement": _rate(sum(1 for a, b in pairs if a == b), len(pairs)),
            "kappa": cohen_kappa(pairs),
            "confusion": dict(Counter(f"사람 {a} / 판사 {b}" for a, b in pairs if a != b)),
        }
    return {
        "by_criterion": out,
        "skipped": dict(skipped),
        "note": "카파 해석의 흔한 기준(Landis & Koch): 0.41~0.60 보통, 0.61~0.80 상당, 0.81 이상 거의 완전. "
        "어느 수준을 근거로 쓸지는 팀이 정한다. 표본이 작으면 값이 크게 흔들린다.",
    }


# --- 키 없이 점검하는 stub 판사 ---------------------------------------------------------------


class StubJudgeClient:
    """API 키 없이 judge 파이프라인을 끝까지 돌려 보는 규칙 판사. 판정 근거로 쓰면 안 된다."""

    model = "stub-judge"
    _FABRICATE = re.compile(r"프로모션|성수기|비수기|계절|행사|경쟁|급증|유행|광고|가격")
    _PRESCRIBE = re.compile(r"추천|권장합니다|넣으세요|넣는 (?:것|편)이 (?:좋|낫)|낫습니다|반드시|걱정 없")

    def complete(self, system: str, user: str, *, max_tokens: int = 400, temperature: float = 0.0) -> LLMResult:
        m = re.search(r"\[설명 문장 — 채점 대상\]\n(.*?)\n\n\[기준\]", user, re.S)
        text = m.group(1) if m else ""

        def item(cid, verdict, reason, ev=""):
            return {"id": cid, "evidence": ev, "reason": reason, "verdict": verdict}

        mentions_basis = bool(re.search(r"예상|팔릴|출고|현재 재고|도착 예정|배수", text))
        added_pending = bool(re.search(r"도착 예정[^.]*?(?:더한|더해|더하)", text))
        out = [
            item(
                "J1",
                "fail" if added_pending else ("pass" if mentions_basis else "na"),
                "stub: 도착 예정 물량을 더했다는 표현" if added_pending else "stub 규칙 판정",
            ),
            item("J2", "pass" if re.search(r"줄이면|늘리면", text) else "na", "stub 규칙 판정"),
            item("J3", "fail" if self._FABRICATE.search(text) else "pass", "stub 규칙 판정"),
            item("J4", "fail" if self._PRESCRIBE.search(text) else "pass", "stub 규칙 판정"),
            item("J5", ("pass" if "추정" in text else "fail") if "위험" in text else "na", "stub 규칙 판정"),
        ]
        raw = json.dumps({"criteria": out}, ensure_ascii=False)
        return LLMResult(raw, self.model, len(system) // 4 + len(user) // 4, len(raw) // 3, 1, None, False)
