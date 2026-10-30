"""모델 선정 · 연동 시험 스크립트 (#32). 가짜 호출기로만 돈다. 기대값은 이석형이 확인한다."""
import json
import re

import pytest

from insight.benchmark import Case, check, draft_text, load_cases, main, percentile, run, summarize
from insight.llm import FakeClient, LLMError, LLMTimeout

CASE = Case("t", True, "user", ["qty_p100"], ["qty_p100"])
OK = json.dumps({"sentences": ["가", "나"]})


def test_fixture_cases():
    system, cases = load_cases()
    assert {c.id for c in cases} == {"normal_custom", "duplicate", "low_sample", "pooled", "no_restock_custom",
                                     "low_grade"}
    assert sum(c.values for c in cases) == sum(not c.values for c in cases) == 6  # 값 포함 · 이름만 6개씩
    key = re.compile(r"\{\{([a-z][a-z0-9_.]*)\}\}")
    for c in cases:
        assert set(key.findall(c.user)) <= set(c.allowed), c.id  # prompt가 안내하는 키는 모두 목록 안에
        assert set(c.required) <= set(c.allowed), c.id
        assert ("[값:" in c.user) == c.values, c.id
    assert "{{qty_p100}}" in system and "{{qty.s100}}" not in system  # 0002 3-4 이름


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"sentences": ["가.", " 나. "]}', "가. 나."),
        ('{"text": "가."}', None),          # 고정본 형식이 아님
        ('{"sentences": ["가.", 1]}', None),
        ("문장", None),                     # JSON 아님
    ],
)
def test_draft_text(raw, expected):
    assert draft_text(raw) == expected


def test_check_format_failure():
    assert check("문장", CASE) == (False, "format")


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([4, 1, 3, 2], 0.5, 3),            # 정렬 1,2,3,4 → round(0.5 × 3) = 2번째 → 3
        (list(range(1, 21)), 0.95, 19),    # round(0.95 × 19) = 18번째 → 19
        ([], 0.5, None),
    ],
)
def test_percentile(values, q, expected):
    assert percentile(values, q) == expected


def test_run_counts_failures_in_latency():
    client = FakeClient([OK, LLMTimeout("slow", 10000), LLMError("down", 50), "문장"], model="gpt-4o-mini",
                        latency_ms=100)
    records = run([client], "sys", [CASE], runs=4)
    s = summarize(records)["by_model"]["gpt-4o-mini"]
    assert s["n_calls"] == 4
    assert (s["timeouts"], s["errors"]) == (1, 1)
    assert s["reasons"] == {"LLM_TIMEOUT": 1, "LLM_ERROR": 1, "format": 1}
    assert s["latency_ms"]["max"] == 10000      # 시간 초과도 응답시간에 들어감
    assert s["cost_usd"] is None                # 실패 건은 비용을 모름 → 합계 없음
    assert s["pass_rate"] is None               # #31 검사기 연결 전


def test_cost_total():
    client = FakeClient([OK, OK], model="gpt-4o-mini")
    s = summarize(run([client], "sys", [CASE], runs=2))["by_model"]["gpt-4o-mini"]
    assert s["cost_usd"]["total"] == round(2 * s["cost_usd"]["per_call"], 6)


def test_main_fake_writes_summary_without_text(tmp_path, capsys):
    out = tmp_path / "r.json"
    main(["--fake", "--runs", "1", "--out", str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["by_model"]) == {"gpt-4o-mini", "gpt-4o"}
    assert len(data["by_case"]) == 2 * 6       # 두 모델 × 값 포함 사례 6개
    assert all("text" not in r for r in data["records"])  # 생성 문장 원문은 저장하지 않음
    assert "| gpt-4o |" in capsys.readouterr().out
