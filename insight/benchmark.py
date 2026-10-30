"""AI-02 모델 선정 · 연동 시험 (#32). 같은 시험 prompt를 모델마다 같은 횟수로 보내 검사 통과율 · 비용 · 응답시간을 잰다.

  python -m insight.benchmark --models gpt-4o-mini,gpt-4o --runs 3 --out result.json   # 실제 호출 (OPENAI_API_KEY)
  python -m insight.benchmark --fake                                                    # 호출 없이 끝까지 도는지 확인

- 로컬 실행용이다. 시험 prompt 고정본은 tests/fixtures/에 있어 배포 이미지에는 없다.
- 집계는 참조 구현 CM-04 1단계와 같게 한다: LLM을 부른 건만, 백분위는 가까운 순위, 비용은 합계.
- 응답시간에는 호출 오류 · 시간 초과 건도 넣는다. 생성 문장 원문은 저장하지 않고 사유 코드만 남긴다.
"""
import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from insight.llm import FakeClient, LLMClient, LLMError, OpenAIClient

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "insight_prompts.json"
DEFAULT_MODELS = ["gpt-4o-mini", "gpt-4o"]


@dataclass
class Case:
    id: str
    values: bool          # 자리 표시자 값을 prompt에 넣었는지 (명세 7장 "실제 숫자 전송 여부")
    user: str
    allowed: list[str]
    required: list[str]


@dataclass
class Record:
    model: str
    case: str
    values: bool
    ok: bool | None       # None = 아직 검사하지 않음
    reason: str | None
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    truncated: bool


def load_cases(path: Path = FIXTURE) -> tuple[str, list[Case]]:
    """(system prompt, 시험 사례)를 고정본에서 읽는다. 만든 방법은 tests/fixtures/insight_prompts.md."""
    # TODO(합의): 아래가 정해지면 고정본 대신 운영 코드로 사례를 만든다 (#32)
    #   - 자리 표시자 이름 · 목록 만들기: #14, 0002 3-4
    #   - 응답 형식 {"text"} / {"sentences"}: IpgoITGO/ipgo-server#25
    #   - 입력 구조(계산 결과 → 인사이트 입력): #10, engine/insight_facts.py
    #   - 값 넣기 · 방향 문구 위치: #24 ①
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["system"], [Case(c["id"], c["values"], c["user"], c["allowed"], c["required"]) for c in data["cases"]]


def draft_text(raw: str) -> str | None:
    """고정본의 응답 형식 {"sentences": [...]}을 문장 하나로 잇는다. 형식이 아니면 None."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    sentences = obj.get("sentences") if isinstance(obj, dict) else None
    if not isinstance(sentences, list) or not all(isinstance(s, str) for s in sentences):
        return None
    return " ".join(s.strip() for s in sentences if s.strip())


def check(raw: str, case: Case) -> tuple[bool | None, str | None]:
    """(통과 여부, 대표 사유 코드)."""
    text = draft_text(raw)
    if text is None:
        return False, "format"
    # #31 병합 뒤 insight.validator.check_draft(text, case.allowed, case.required)로 검사한다 (#32)
    return None, None


def run(clients: list[LLMClient], system: str, cases: list[Case], runs: int) -> list[Record]:
    records = []
    for client in clients:
        for case in cases:
            for _ in range(runs):
                try:
                    r = client.complete(system, case.user)
                except LLMError as e:
                    records.append(Record(client.model, case.id, case.values, False, e.reason.value, e.latency_ms,
                                          0, 0, None, False))
                    continue
                ok, reason = check(r.text, case)
                records.append(Record(client.model, case.id, case.values, ok, reason, r.latency_ms,
                                      r.input_tokens, r.output_tokens, r.cost_usd, r.truncated))
    return records


def percentile(values: list[int], q: float) -> int | None:
    """가까운 순위 백분위 (참조 구현 evaluation/bank.py `_p`와 같은 방식)."""
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(q * (len(v) - 1))))]


def _stats(rs: list[Record]) -> dict:
    checked = [r for r in rs if r.ok is not None]
    lat = [r.latency_ms for r in rs]
    costs = [r.cost_usd for r in rs]
    return {
        "n_calls": len(rs),
        "pass_rate": round(sum(r.ok for r in checked) / len(rs), 3) if rs and len(checked) == len(rs) else None,
        "reasons": dict(Counter(r.reason for r in rs if r.reason).most_common()),
        "latency_ms": {"p50": percentile(lat, 0.5), "p95": percentile(lat, 0.95), "max": max(lat, default=None)},
        "tokens_mean": {"input": round(sum(r.input_tokens for r in rs) / len(rs)) if rs else None,
                        "output": round(sum(r.output_tokens for r in rs) / len(rs)) if rs else None},
        "cost_usd": ({"total": round(sum(costs), 6), "per_call": round(sum(costs) / len(rs), 6)}
                     if rs and all(c is not None for c in costs) else None),
        "errors": sum(r.reason == "LLM_ERROR" for r in rs),
        "timeouts": sum(r.reason == "LLM_TIMEOUT" for r in rs),
        "truncated": sum(r.truncated for r in rs),
    }


def summarize(records: list[Record]) -> dict:
    models = list(dict.fromkeys(r.model for r in records))
    cases = list(dict.fromkeys((r.case, r.values) for r in records))
    return {
        "by_model": {m: _stats([r for r in records if r.model == m]) for m in models},
        "by_case": [{"model": m, "case": c, "values": v,
                     **_stats([r for r in records if (r.model, r.case, r.values) == (m, c, v)])}
                    for m in models for c, v in cases],
    }


def _fmt(x, unit: str = "") -> str:
    return "—" if x is None else f"{x:,}{unit}" if isinstance(x, int) else f"{x}{unit}"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def print_tables(summary: dict) -> None:
    """결과 문서에 그대로 옮길 수 있게 Markdown 표로 출력한다."""
    print("| 모델 | 사례 | 값 | 통과율 | 주요 사유 | 응답시간 p50 · p95 | 평균 토큰 (입력 + 출력) |")
    print("|---|---|---|---|---|---|---|")
    for s in summary["by_case"]:
        top = ", ".join(f"{k} {v}" for k, v in list(s["reasons"].items())[:3]) or "—"
        lat, tok = s["latency_ms"], s["tokens_mean"]
        print(f"| {s['model']} | {s['case']} | {'포함' if s['values'] else '이름만'} | {_pct(s['pass_rate'])} | {top} "
              f"| {_fmt(lat['p50'])} · {_fmt(lat['p95'], 'ms')} | {_fmt(tok['input'])} + {_fmt(tok['output'])} |")
    print()
    print("| 모델 | 호출 | 통과율 | 건당 비용 | 합계 비용 | 응답시간 p50 · p95 · 최대 | 호출 오류 | 시간 초과 | 잘림 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for m, s in summary["by_model"].items():
        lat, cost = s["latency_ms"], s["cost_usd"] or {}
        latency = f"{_fmt(lat['p50'])} · {_fmt(lat['p95'])} · {_fmt(lat['max'], 'ms')}"
        print(f"| {m} | {s['n_calls']} | {_pct(s['pass_rate'])} | {_fmt(cost.get('per_call'), ' USD')} "
              f"| {_fmt(cost.get('total'), ' USD')} | {latency} | {s['errors']} | {s['timeouts']} | {s['truncated']} |")


def _fake_clients(models: list[str], n: int) -> list[FakeClient]:
    sentences = ["가짜 응답입니다.", "실제 호출 없이 측정 흐름만 확인합니다."]
    draft = json.dumps({"sentences": sentences}, ensure_ascii=False)
    return [FakeClient([draft] * n, model=m) for m in models]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="AI 인사이트 모델 선정 · 연동 시험 (#32)")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS), help="쉼표로 구분한 모델 이름")
    ap.add_argument("--runs", type=int, default=3, help="사례당 호출 횟수 (기본 3회)")
    ap.add_argument("--case", help="특정 사례만 (예: normal_custom)")
    ap.add_argument("--no-values", action="store_true", help="자리 표시자 이름만 넣은 판으로 측정")
    ap.add_argument("--fake", action="store_true", help="가짜 호출기로 실행 (API 호출 · 비용 없음)")
    ap.add_argument("--out", type=Path, help="요약 JSON을 저장할 경로 (생성 문장 원문은 넣지 않음)")
    ap.add_argument("--fixture", type=Path, default=FIXTURE)
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 표의 —, · 출력

    system, cases = load_cases(args.fixture)
    cases = [c for c in cases if c.values != args.no_values and (args.case is None or c.id == args.case)]
    if not cases:
        ap.error("고른 조건에 맞는 사례가 없습니다")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    clients = _fake_clients(models, len(cases) * args.runs) if args.fake else [OpenAIClient(model=m) for m in models]

    records = run(clients, system, cases, args.runs)
    summary = summarize(records)
    print_tables(summary)
    if args.out:
        meta = {"fake": args.fake, "runs": args.runs, "values": not args.no_values, "fixture": args.fixture.name}
        args.out.write_text(json.dumps({"meta": meta, **summary, "records": [asdict(r) for r in records]},
                                       ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
