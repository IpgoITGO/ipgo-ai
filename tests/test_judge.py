"""LLM 판사 (insight.evaluation.judge, #54). 참조 구현 tests/test_judge.py를 0002 이름 · 판사 호출기에 맞게 옮겼다.

사전 등록 잠금(rule_fingerprint) 테스트는 2단계라 옮기지 않았다.
"""

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from insight.evaluation.cases import EvalCase
from insight.evaluation.config import load_config
from insight.evaluation.judge import (
    CRITERIA,
    StubJudgeClient,
    Target,
    agreement,
    build_judge_prompt,
    cohen_kappa,
    coverage_checks,
    judge_report,
    parse_judge,
    run_judge,
    select_targets,
    write_review_csv,
)
from insight.evaluation.judge_client import JudgeClient
from insight.llm import FakeClient, LLMTimeout
from insight.prompt import SYSTEM_PROMPT

SAMPLES = Path(__file__).resolve().parents[1] / "insight" / "evaluation" / "samples"
CFG = load_config()
BASIS = CFG.judge.basis_keys
GOOD_TEMPLATE = " ".join(
    json.loads(next(ln for ln in SYSTEM_PROMPT.splitlines() if ln.startswith('{"sentences"')))["sentences"]
)  # (가) 정상 예시


def _case(name: str) -> EvalCase:
    return EvalCase.model_validate(json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8")))


@pytest.fixture
def normal():
    return _case("normal")


def verdict_json(**v):
    """v: J1="pass" ... 지정 안 한 기준은 pass."""
    return json.dumps(
        {"criteria": [{"id": c.id, "evidence": "", "reason": "r", "verdict": v.get(c.id, "pass")} for c in CRITERIA]},
        ensure_ascii=False,
    )


# --- 포함 여부 검사 (코드) ---------------------------------------------------------------------


def test_coverage_good_template(normal):
    cov = coverage_checks(GOOD_TEMPLATE, normal, BASIS)
    assert cov["C1"] is True and cov["C2"] is True and cov["C5"] is True
    assert cov["C3"] is False  # 정상 예시는 120%를 다루지 않는다
    assert cov["C4"] is False  # 정상 예시는 근거 수치 키를 쓰지 않는다 (참조 구현 README "평가 중 발견한 점" C4 0개)


def test_coverage_missing_parts(normal):
    t = "권장 입고량은 {{qty_p100}}개이고 {{qty_p120}}개로 {{action_p120}}"
    cov = coverage_checks(t, normal, BASIS)
    assert cov == {"C1": True, "C2": False, "C3": True, "C4": False, "C5": False}


def test_coverage_not_applicable(normal):
    assert set(coverage_checks(None, normal, BASIS).values()) == {None}  # 기본 문장
    assert set(coverage_checks(GOOD_TEMPLATE, _case("cannot_compute"), BASIS).values()) == {None}  # 계산 불가


# --- 판사 프롬프트 · 파싱 ----------------------------------------------------------------------


def test_prompt_contains_table_and_text_and_reverses(normal):
    _, u0 = build_judge_prompt(normal, "문장A", reverse=False)
    _, u1 = build_judge_prompt(normal, "문장A", reverse=True)
    assert "[설명 문장 — 채점 대상]\n문장A" in u0 and "[계산 결과 표" in u0
    ids0 = [c.id for c in CRITERIA if f"- {c.id} " in u0]
    assert u0.index("- J1 ") < u0.index("- J5 ") and u1.index("- J5 ") < u1.index("- J1 ")
    assert len(ids0) == len(CRITERIA)


def test_parse_rejects_na_where_not_allowed():
    raw = verdict_json(J3="na")
    got, err = parse_judge("앞말 " + raw + " 뒷말")  # 앞뒤 잡문이 있어도 JSON을 찾는다
    assert "J3" not in got and "J3" in err
    assert parse_judge("판정 불가")[1] == "JSON 아님"


# --- 2회 판정 결합 ---------------------------------------------------------------------------


def _target(normal, text="문장", template=GOOD_TEMPLATE):
    return Target("experiment", normal.id, "experiment", text, template, normal)


def test_two_runs_agree_and_disagree(normal):
    client = FakeClient([verdict_json(J4="fail"), verdict_json(J4="fail", J2="na")], model="judge-x")
    row = run_judge([_target(normal)], client, runs=2)[0]
    assert row["judge"]["J4"]["verdict"] == "fail"  # 두 번 모두 fail → 확정
    assert row["judge"]["J2"]["verdict"] == "disagree"  # pass / na → 불일치
    assert row["judge"]["J1"]["verdict"] == "pass"
    assert client.calls[0][1].index("- J1 ") < client.calls[0][1].index("- J5 ")  # 두 번째는 기준 순서 반대
    assert client.calls[1][1].index("- J5 ") < client.calls[1][1].index("- J1 ")


def test_one_run_invalid_is_not_confirmed(normal):
    row = run_judge([_target(normal)], FakeClient(["이건 JSON 이 아님", verdict_json()]), runs=2)[0]
    assert all(v["verdict"] == "disagree" for v in row["judge"].values())
    assert row["runs"][0]["error"] == "JSON 아님"


def test_timeouts_become_error(normal):
    row = run_judge([_target(normal)], FakeClient([LLMTimeout("t", 10), LLMTimeout("t", 10)]), runs=2)[0]
    assert all(v["verdict"] == "error" for v in row["judge"].values())


# --- 보고서 · 사람 채점 ------------------------------------------------------------------------


def test_report_and_review_roundtrip(normal, tmp_path):
    client = FakeClient(
        [verdict_json(J3="fail"), verdict_json(J3="fail"), verdict_json(), verdict_json()], model="gpt-5.6-sol"
    )
    targets = [_target(normal, "a"), Target("offline", normal.id, "0", "b", "{{qty_p100}}", normal)]
    log = tmp_path / "eval" / "judge_calls.jsonl"  # 폴더가 없어도 만든다 (#54 11/4)
    rows = run_judge(targets, client, runs=2, basis_keys=BASIS, log_path=log)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2

    rep = judge_report(
        rows,
        judge_model="gpt-5.6-sol",
        generator_versions={"model": "gpt-4o-mini-2024-07-18"},
        runs=2,
        client_kind="openai",
    )
    assert rep["criteria"]["J3"]["fail"] == 1 and rep["criteria"]["J3"]["pass_rate"] == 0.5
    assert rep["criteria"]["J3"]["below_advisory"] is True
    assert rep["coverage"]["C1"] == {**rep["coverage"]["C1"], "applicable": 2, "covered": 2}
    assert rep["texts_all_clear"] == 0
    assert not any("같은 계열" in n for n in rep["notes"])

    csv_path = tmp_path / "review.csv"
    write_review_csv(rows, csv_path)
    assert len(csv_path.read_text(encoding="utf-8-sig").splitlines()) == 1 + 2 * len(CRITERIA)
    with csv_path.open(encoding="utf-8-sig") as f:
        recs = list(csv.DictReader(f))
    for r in recs:  # 사람이 J3를 둘 다 fail로 채점했다고 가정 → J3 일치 1/2
        r["human_verdict"] = "fail" if r["criterion"] == "J3" else r["judge_verdict"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)
    ag = agreement(csv_path)
    assert ag["by_criterion"]["J3"]["n"] == 2 and ag["by_criterion"]["J3"]["agreement"] == 0.5
    assert ag["by_criterion"]["J1"]["agreement"] == 1.0


def test_same_family_warning():
    rep = judge_report(
        [],
        judge_model="gpt-4o-mini-2024-07-18",
        generator_versions={"model": "gpt-4o-mini-2024-07-18"},
        runs=2,
        client_kind="openai",
    )
    assert any("같은 계열" in n for n in rep["notes"])


def test_cohen_kappa_known_values():
    pairs = [("pass", "pass")] * 4 + [("fail", "fail")] * 4 + [("pass", "fail"), ("fail", "pass")]
    assert cohen_kappa(pairs) == 0.6
    assert cohen_kappa([("pass", "pass")] * 3) == 1.0
    assert cohen_kappa([]) is None


# --- 대상 고르기 · stub 점검 -------------------------------------------------------------------


def test_select_targets_filters_versions_and_scope(normal, tmp_path):
    v = {"prompt": "p1", "fallback": "f1", "model": "m1"}
    d = normal.model_dump(mode="json", by_alias=True)
    prepared = {"versions": v, "bank": {"results": [d], "all_results": [d]}}
    recs = [
        {"calc_id": normal.id, "run": "experiment", "path": "llm", "text": "옛", "versions": v},
        {"calc_id": normal.id, "run": "experiment", "path": "llm", "text": "새", "versions": v},
        {"calc_id": normal.id, "run": 0, "path": "llm", "text": "오프", "versions": v},
        {"calc_id": normal.id, "run": "experiment", "path": "fixed", "text": "고정", "versions": v},
        {"calc_id": normal.id, "run": "experiment", "path": "llm", "text": "다른버전", "versions": v | {"model": "m0"}},
    ]
    log = tmp_path / "calls.jsonl"
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs[:4]) + "\n", encoding="utf-8")
    assert [t.text for t in select_targets(prepared, log)] == ["새"]  # 같은 키는 마지막 기록, fixed 제외
    assert [t.text for t in select_targets(prepared, log, scope="offline")] == ["오프"]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs[4:]) + "\n", encoding="utf-8")
    assert select_targets(prepared, log) == []
    assert len(select_targets(prepared, log, all_versions=True)) == 1


def test_stub_judge_end_to_end(normal):
    rows = run_judge([_target(normal, "성수기라 많이 팔릴 것으로 보여 넣는 것이 좋습니다.")], StubJudgeClient(), runs=2)
    j = rows[0]["judge"]
    assert j["J3"]["verdict"] == "fail" and j["J4"]["verdict"] == "fail"


# --- 판사 호출기 (#54 11/3 시험 호출) -----------------------------------------------------------


class _FakeSDK:
    def __init__(self, content: str, finish: str = "stop"):
        self.kwargs = None
        self.content, self.finish = content, finish
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.kwargs = kw
        return SimpleNamespace(
            model="gpt-5.6-sol",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[SimpleNamespace(finish_reason=self.finish, message=SimpleNamespace(content=self.content))],
        )


def test_judge_client_sends_max_completion_tokens_without_temperature():
    sdk = _FakeSDK(verdict_json())
    r = JudgeClient("gpt-5.6-sol", sdk=sdk).complete("s", "u", max_tokens=1200, temperature=0.0)
    assert sdk.kwargs["max_completion_tokens"] == 1200
    assert "temperature" not in sdk.kwargs and "max_tokens" not in sdk.kwargs
    assert r.model == "gpt-5.6-sol" and r.cost_usd is None  # 단가 환경 변수가 없으면 비용 모름


def test_truncated_judge_output_is_error(normal):
    client = JudgeClient("gpt-5.6-sol", sdk=_FakeSDK("", finish="length"))
    row = run_judge([_target(normal)], client, runs=2)[0]
    assert all(v["verdict"] == "error" for v in row["judge"].values())
    assert row["runs"][0]["error"].startswith("출력 잘림")
