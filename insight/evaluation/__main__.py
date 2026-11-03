"""명령줄 진입점:  python -m insight.evaluation <명령> --help

참조 구현(ai_insight v0.3) evaluation/__main__.py에서 1단계 명령만 옮겼다 (#54).
  make-bank → prepare(오프라인 관문) → judge(LLM 판사) → judge-agree(사람 채점 일치율)
2단계(lock · balance · serve · simulate · pilot-llm · analyze)는 #54 제외 범위.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .bank import load_json, make_bank, prepare, save_json
from .cases import EvalCase
from .config import DEFAULT_PATH, load_config

D = "data/eval"  # .gitignore의 data/ 아래 (커밋되지 않음)
SAMPLES = Path(__file__).with_name("samples")


def _cfg(a):
    return load_config(a.config)


def build_client(kind: str):
    """생성용 호출기. 서비스(/engine/insight)와 같은 insight.llm.OpenAIClient · .env 설정 (같은 조건으로 평가)."""
    from insight.llm import OpenAIClient

    from .stub_client import StubClient

    if kind == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY 가 없습니다 (.env). 먼저 --client stub 으로 파이프라인을 점검하세요.")
        return OpenAIClient()
    return StubClient()


def _load_cases(path: str | Path) -> list[EvalCase]:
    """EvalCase JSON 배열 · JSON Lines 파일, 또는 *.json 이 든 폴더 (samples/ 형식)."""
    p = Path(path)
    if p.is_dir():
        items = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(p.glob("*.json"))]
    else:
        text = p.read_text(encoding="utf-8").strip()
        items = json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x.strip()]
        items = items if isinstance(items, list) else [items]
    return [EvalCase.model_validate(x) for x in items]


def cmd_make_bank(a):
    cfg = _cfg(a)
    if a.engine:
        cases, source = _load_cases(a.engine), f"engine:{a.engine}"
    else:
        from .mock_engine import mock_bank

        cases = mock_bank(a.mock, seed=a.seed, low_grade_rate=cfg.mock.low_grade_rate)
        source = f"mock(n={a.mock}, seed={a.seed}, low_grade_rate={cfg.mock.low_grade_rate})"
    if a.add_samples:
        cases += _load_cases(SAMPLES)
    bank = make_bank(cases, cfg, source)
    save_json(bank, Path(a.out))
    print(f"시나리오 은행 저장: {a.out}")
    print(
        f"  전체 {len(cases)}개, 연습 {len(bank['practice'])}개, 세트 "
        + ", ".join(f"{k}:{len(v)}개" for k, v in bank["sets"].items())
        + f", 실험 제외 {len(bank['excluded'])}개"
    )


def cmd_prepare(a):
    cfg = _cfg(a)
    bank = load_json(Path(a.bank))
    prepared = prepare(bank, cfg, build_client(a.client), Path(a.call_log), runs=a.runs)
    save_json(prepared, Path(a.out))
    o, v = prepared["offline_report"], prepared["versions"]
    print(f"실험 자료 저장: {a.out}")
    print(f"  평가 대상: {v.get('prompt')} · 기본 문장 {v.get('fallback')} · 모델 {v.get('model')}")
    print(
        f"  생성 {o['n_generations']}회 (LLM 호출 {o['n_llm_called']}), 검사 통과율 {o['validation_pass_rate']}, "
        f"사유 {o['reason_codes']}, 수치 불일치 {len(o['final_text_number_mismatch'])}건, "
        f"p50 {o['latency_ms']['p50']}ms"
    )
    print(f"  오프라인 관문: {'통과' if o['gate']['passed'] else '미통과 → 사람 실험 없이 고정 문장으로 축소 판정'}")
    if a.client == "stub":
        print("  ※ stub 문장은 파이프라인 점검용입니다. 본 실험 전에 --client openai 로 다시 준비하세요.")


def cmd_judge(a):
    from .judge import StubJudgeClient, judge_report, run_judge, select_targets, write_review_csv
    from .judge_client import JudgeClient

    cfg = _cfg(a)
    jc = cfg.judge
    prepared = load_json(Path(a.prepared))
    targets = select_targets(prepared, Path(a.call_log), scope=a.scope or jc.scope, all_versions=a.all_versions)
    if a.limit:
        targets = targets[: a.limit]
    if not targets:
        raise SystemExit(
            "판정할 문장이 없습니다. prepare 를 먼저 실행했는지, 호출 로그의 버전이 prepared.json 과 같은지 확인하세요 "
            "(--all-versions 로 버전 무시)."
        )
    if a.client == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY 가 없습니다 (.env). 먼저 --client stub 으로 점검하세요.")
        model = a.model or os.getenv("JUDGE_MODEL") or jc.model
        client = JudgeClient(model=model, timeout_sec=float(os.getenv("JUDGE_TIMEOUT_SEC", jc.timeout_sec)))
    else:
        model, client = StubJudgeClient.model, StubJudgeClient()
    runs = a.runs or jc.runs
    out = Path(a.out_dir)
    rows = run_judge(
        targets, client, runs=runs, max_tokens=jc.max_tokens, basis_keys=jc.basis_keys, log_path=Path(a.judge_log)
    )
    rep = judge_report(
        rows,
        judge_model=model,
        generator_versions=prepared.get("versions") or {},
        runs=runs,
        client_kind=a.client,
        advisory_min_pass_rate=jc.advisory_min_pass_rate,
    )
    save_json(rep, out / "judge_report.json")
    write_review_csv(rows, out / "judge_review.csv")
    print(
        f"LLM 판사: {rep['n_texts']}개 문장 ({rep['by_scope']}), 판사 {model} × {runs}회, "
        f"프롬프트 {rep['judge']['prompt_version']}"
    )
    print("  포함 여부 검사 (코드):")
    for cid, c in rep["coverage"].items():
        rate = "해당 없음" if c["rate"] is None else f"{c['covered']}/{c['applicable']} ({c['rate']:.0%})"
        print(f"    {cid} {c['name']}: {rate}")
    print("  의미 판정 (LLM 판사, 확정 판정 기준 통과율):")
    for cid, c in rep["criteria"].items():
        rate = "—" if c["pass_rate"] is None else f"{c['pass_rate']:.0%}"
        flag = "  ← 참고 기준 미만" if c["below_advisory"] else ""
        print(
            f"    {cid} {c['name']}: pass {c['pass']} · fail {c['fail']} · na {c['na']} · 불일치 {c['disagree']} · "
            f"오류 {c['error']} → {rate}{flag}"
        )
    print(f"  모든 항목 통과 문장: {rep['texts_all_clear']}/{rep['n_texts']}")
    for n in rep["notes"]:
        print(f"  ※ {n}")
    print(f"보고서: {out / 'judge_report.json'}, 사람 채점표: {out / 'judge_review.csv'}, 로그: {a.judge_log}")


def cmd_judge_agree(a):
    from .judge import agreement

    res = agreement(Path(a.labels))
    save_json(res, Path(a.out))
    for cid, c in res["by_criterion"].items():
        ag = "—" if c["agreement"] is None else f"{c['agreement']:.0%}"
        print(f"{cid} {c['name']}: 표본 {c['n']}, 일치율 {ag}, 카파 {c['kappa'] if c['kappa'] is not None else '—'}")
    if res["skipped"]:
        print("제외: " + ", ".join(f"{k} {n}" for k, n in res["skipped"].items()))
    print(res["note"])
    print(f"저장: {a.out}")


def main(argv=None):
    load_dotenv()  # 로컬 .env (app/config.py · insight/benchmark.py와 같은 방식)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)
    p = argparse.ArgumentParser(
        prog="python -m insight.evaluation", description="CM-04 AI 인사이트 효과 평가 1단계 (#54)"
    )
    p.add_argument("--config", default=str(DEFAULT_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("make-bank", help="계산 결과로 시나리오 은행(세트) 만들기")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--engine", help="계산 결과(EvalCase) JSON 배열 · JSONL 파일 또는 *.json 폴더")
    g.add_argument("--mock", type=int, help="가짜 결과 N개 (도구 점검용)")
    s.add_argument("--add-samples", action="store_true", help="samples/ 6종도 오프라인 평가 대상에 넣기")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out", default=f"{D}/bank.json")
    s.set_defaults(fn=cmd_make_bank)

    s = sub.add_parser("prepare", help="서비스 경로로 고정 · AI 문장과 문항을 미리 만들고 오프라인 평가")
    s.add_argument("--bank", default=f"{D}/bank.json")
    s.add_argument("--client", choices=["stub", "openai"], default="stub")
    s.add_argument("--runs", type=int, default=1, help="오프라인 평가에서 결과당 생성 반복 횟수")
    s.add_argument("--out", default=f"{D}/prepared.json")
    s.add_argument("--call-log", default=f"{D}/insight_calls.jsonl")
    s.set_defaults(fn=cmd_prepare)

    s = sub.add_parser("judge", help="LLM 판사: AI 작성 문장의 포함 여부(코드)와 의미(LLM)를 채점 — 참고 지표")
    s.add_argument("--prepared", default=f"{D}/prepared.json")
    s.add_argument("--call-log", default=f"{D}/insight_calls.jsonl")
    s.add_argument("--client", choices=["stub", "openai"], default="stub")
    s.add_argument("--model", help="판사 모델 (기본: JUDGE_MODEL 또는 eval.yaml judge.model)")
    s.add_argument("--runs", type=int, help="같은 문장 판정 횟수 (기본: eval.yaml judge.runs)")
    s.add_argument("--scope", choices=["experiment", "offline", "all"], help="기본: eval.yaml judge.scope")
    s.add_argument("--all-versions", action="store_true", help="prepared.json 과 버전이 다른 호출 기록도 포함")
    s.add_argument("--limit", type=int, help="앞에서 N개만 (비용 점검용)")
    s.add_argument("--out-dir", default=f"{D}/out")
    s.add_argument("--judge-log", default=f"{D}/judge_calls.jsonl")
    s.set_defaults(fn=cmd_judge)

    s = sub.add_parser("judge-agree", help="사람 채점표(judge_review.csv)와 판사 판정의 일치율 · 카파")
    s.add_argument("--labels", default=f"{D}/out/judge_review.csv")
    s.add_argument("--out", default=f"{D}/out/judge_agreement.json")
    s.set_defaults(fn=cmd_judge_agree)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
