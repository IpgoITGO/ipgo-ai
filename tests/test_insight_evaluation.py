"""CM-04 효과 평가 1단계 (#54). 참조 구현 tests/test_evaluation.py 중 1단계 테스트를 0002 · 서비스 경로로 옮겼다.

2단계(역균형 · 실험 서버 · 시뮬레이션 · 채택 규칙 · 잠금) 테스트는 #54 제외 범위라 옮기지 않았다.
CI는 실제 API를 부르지 않는다.
기대값은 참조 구현 samples/의 값이다 (이석형 확인).
"""

import json
import re
from pathlib import Path

import pytest

from engine.insight_facts import to_insight_input
from insight.evaluation.bank import experiment_candidate, for_experiment, make_bank, prepare
from insight.evaluation.cases import EvalCase, all_values, from_reference, to_input, verify_filled
from insight.evaluation.config import load_config
from insight.evaluation.mock_engine import mock_bank
from insight.evaluation.quiz import make_questions
from insight.evaluation.stub_client import StubClient
from insight.evaluation.table import render_table
from insight.llm import FakeClient
from insight.prompt import SYSTEM_PROMPT, build_prompt
from schemas.base import ResultStatus, RiskStatus, ScenarioType

SAMPLES = Path(__file__).resolve().parents[1] / "insight" / "evaluation" / "samples"
CFG = load_config()
# system prompt (가) 정상 예시 그대로 (참조 구현 conftest GOOD_NORMAL은 4문장이라 저장소 규칙 2~3문장에 맞지 않음)
GOOD_NORMAL = next(ln for ln in SYSTEM_PROMPT.splitlines() if ln.startswith('{"sentences"'))


def _case(name: str) -> EvalCase:
    return EvalCase.model_validate(json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8")))


@pytest.fixture
def normal():
    return _case("normal")


def _bank(c: EvalCase) -> dict:
    return {
        "all_results": [c.model_dump(mode="json", by_alias=True)],
        "results": [for_experiment(c).model_dump(mode="json", by_alias=True)],
        "sets": {"0": [], "1": [], "2": []},
        "practice": [c.id],
    }


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    bank = make_bank(mock_bank(40, seed=7), CFG, "mock")
    return prepare(bank, CFG, StubClient(bad_rate=0.0), tmp_path_factory.mktemp("p") / "calls.jsonl")


# --- 0002 형식 변환 ---------------------------------------------------------------------------


def test_samples_are_reference_values_in_0002_names(normal):
    r = normal.result
    assert r.status == ResultStatus.OK and r.risk_status == RiskStatus.OK
    assert r.recommended_qty == 290 and r.forecast_outbound_qty == 410 and r.pre_arrival_risk == 0.05
    assert [s.type for s in r.scenarios] == [
        ScenarioType.P80,
        ScenarioType.P100,
        ScenarioType.P120,
        ScenarioType.CUSTOM,
    ]
    assert _case("no_restock").result.status == ResultStatus.NOT_NEEDED
    assert _case("cannot_compute").result.status == ResultStatus.NOT_CALCULABLE
    low = _case("low_sample")
    assert (
        low.result.risk_status == RiskStatus.INSUFFICIENT_SAMPLE and low.method_fallback and low.risk_basis == "POOLED"
    )


def test_input_builds_service_prompt(normal):
    p = build_prompt(to_input(normal))
    assert p.required == ["qty_p100", "risk_post_p100", "risk_pre"]
    assert "[값:" not in p.user  # 서비스와 같이 이름만 보낸다


def test_input_is_service_converter(normal):
    """입력은 #59 to_insight_input() 그대로, EvalCase의 계산 밖 값은 #58 필드로 (#54 11/4)."""
    inp = to_input(normal)
    r = normal.result.model_copy(
        update={"method_fallback": False, "risk_basis": "SKU", "no_date_receipt_qty": normal.no_date_receipt_qty}
    )
    assert inp == to_insight_input(r, "(이 상품)", as_of_date=normal.as_of_date)
    assert normal.no_date_receipt_qty > 0 and inp.has_no_date_receipts and "no_date_receipts" in inp.placeholders
    assert inp.scenarios  # #58 scenarios도 채움


def test_verify_filled_catches_foreign_numbers():
    values = {"qty_p100": "290", "risk_pre": "5%"}
    assert verify_filled("권장 290개, 위험 5%.", values) == (True, None)
    assert verify_filled("권장 300개.", values)[0] is False
    assert verify_filled("권장 {{qty_p100}}개.", values)[0] is False


def test_mock_low_grade_needs_range_rule():
    with pytest.raises(SystemExit):
        mock_bank(5, low_grade_rate=0.2)
    assert all(c.result.risk_status != RiskStatus.LOW_GRADE for c in mock_bank(5))


def test_from_reference_maps_duplicate_and_pending():
    c = from_reference(
        {
            "calc_id": "x",
            "sku": {"barcode": "1"},
            "base_date": "2026-06-15",
            "status": "NORMAL",
            "inputs": {
                "cover_days": 34,
                "current_stock": 10,
                "pending_orders": [{"qty": 5}, {"qty": 7}],
                "unreflected_pending_qty": 3,
            },
            "forecast": {"method": "A", "cover_demand": 40, "multiplier": 1.05},
            "recommendation": {"qty": 50},
            "stockout_risk_before_arrival": {"computed": True, "risk": 0.1, "sample_size": 40},
            "scenarios": [
                {
                    "key": "s80",
                    "qty": 50,
                    "remaining_at_next_restock": 20,
                    "duplicate_of": "s100",
                    "stockout_risk_after_arrival": {"computed": True, "risk": 0.06},
                },
                {
                    "key": "s100",
                    "qty": 50,
                    "remaining_at_next_restock": 20,
                    "stockout_risk_after_arrival": {"computed": True, "risk": 0.06},
                },
            ],
        }
    )
    assert c.result.planned_receipts_in_horizon == 12 and c.no_date_receipt_qty == 3
    assert c.scenario(ScenarioType.P80).is_duplicate and not c.method_fallback



def test_display_rules_follow_server(normal):
    """소수 재고 · 수요는 서버 DisplayQuantities와 같게 보인다 (#49 f80e67b).

    재고 내림, 예상 출고량 · 미충족 수요 올림.
    """
    r = normal.result
    p80, p100 = r.scenarios[0], r.scenarios[1]
    frac = normal.model_copy(update={"result": r.model_copy(update={
        "forecast_outbound_qty": 409.2, "start_stock_qty": 120.9,
        "scenarios": [p80.model_copy(update={"expected_ending_stock": 19.9, "unmet_demand_qty": 0.2}),
                      p100.model_copy(update={"expected_ending_stock": 19.1})] + r.scenarios[2:]})})
    facts = dict(render_table(for_experiment(frac))["facts"])
    assert facts["커버 기간 예상 출고"] == "410개" and facts["현재 재고"] == "120개"
    v = all_values(frac)
    assert v["ending_p80"] == "19" and v["unmet_p80"] == "1"
    assert to_input(frac).directions["ending_p80_vs_p100"] == "같음"  # 둘 다 19개로 보임

# --- 실험 대상 선정 (참조 구현 test_candidate_rules · test_table_matches_engine_values) -----------


def test_candidate_rules(normal):
    assert experiment_candidate(normal)[0] and experiment_candidate(_case("pooled"))[0]
    assert "중복" in experiment_candidate(_case("duplicate"))[1]
    assert "미산출" in experiment_candidate(_case("low_sample"))[1]
    assert not experiment_candidate(_case("no_restock"))[0] and not experiment_candidate(_case("cannot_compute"))[0]
    # 실험 화면은 직접 입력(custom)을 빼고 80 · 100 · 120%만
    assert [s.type for s in for_experiment(normal).result.scenarios] == [
        ScenarioType.P80,
        ScenarioType.P100,
        ScenarioType.P120,
    ]


def test_table_matches_engine_values(normal):
    t = render_table(for_experiment(normal))
    facts = dict(t["facts"])
    assert facts["권장 입고량"] == "290개" and facts["커버 기간 예상 출고"] == "410개"
    assert facts["도착 전 품절 위험 (모든 시나리오 공통)"] == "5% (표본 42건)"
    assert t["rows"][0] == ["80%", "240개", "7월 12일", "0개", "25%"]


# --- 문장 준비: 서비스 경로 그대로 (참조 구현은 InsightService) ------------------------------------


def test_prepare_uses_service_outputs(normal, tmp_path):
    p = prepare(_bank(normal), CFG, FakeClient([GOOD_NORMAL, GOOD_NORMAL]), tmp_path / "c.jsonl")
    item = p["items"][normal.id]
    assert item["llm_source"] == "llm" and "290개" in item["llm_text"] and item["fixed_text"] != item["llm_text"]
    assert p["versions"]["prompt"].startswith("prompt-") and p["versions"]["model"] == "fake-model"
    assert p["offline_report"]["validation_pass_rate"] == 1.0


def test_failed_draft_becomes_fallback_in_llm_condition(normal, tmp_path):
    bad = '{"sentences": ["권장 입고량은 290개입니다.", "가장 적절합니다."]}'
    p = prepare(_bank(normal), CFG, FakeClient([bad, bad]), tmp_path / "c.jsonl")
    item = p["items"][normal.id]
    assert item["llm_source"] == "fixed_fallback" and item["llm_text"] == item["fixed_text"]
    assert item["llm_reason_code"] == "raw_number"
    assert p["offline_report"]["gate"]["passed"] is False


def test_not_ok_results_do_not_call_llm(tmp_path):
    c = _case("no_restock")
    bank = {"all_results": [c.model_dump(mode="json", by_alias=True)], "results": [], "sets": {}, "practice": []}
    client = FakeClient()
    p = prepare(bank, CFG, client, tmp_path / "c.jsonl")
    assert client.calls == [] and p["offline_report"]["n_skipped_fixed"] == 1  # Spring은 OK 결과만 부른다 (#24)


def test_null_input_does_not_call_llm(normal, tmp_path):
    """#59가 None(생성 보류)을 주면 LLM을 부르지 않고 관문 분모에도 넣지 않는다."""
    c = normal.model_copy(update={"result": normal.result.model_copy(update={"method": None})})
    assert to_input(c) is None
    bank = {"all_results": [c.model_dump(mode="json", by_alias=True)], "results": [], "sets": {}, "practice": []}
    client = FakeClient()
    rep = prepare(bank, CFG, client, tmp_path / "c.jsonl")["offline_report"]
    assert client.calls == [] and rep["n_generations"] == 1 and rep["n_llm_called"] == 0  # 산식 없음은 기본 문장도 없음


def test_stub_drafts_pass_real_validator(prepared):
    assert prepared["offline_report"]["validation_pass_rate"] == 1.0
    assert prepared["offline_report"]["final_text_number_mismatch"] == []


def test_stub_comparison_keeps_ending_value(normal):
    """시나리오 비교 문장에 잔여 재고 값 (#44 기준 구현 other_risk, #53 v0.6 예시와 같은 형태)."""
    p = build_prompt(to_input(normal))
    sents = json.loads(StubClient(bad_rate=0.0).complete(p.system, p.user).text)["sentences"]
    comp = [s for s in sents if "_connect}}" in s]
    want = re.compile(r"남는 재고는 \{\{ending_(p\d+)\}\}개로 \{\{dir_ending_\1_connect\}\}")
    assert comp and all(want.search(s) for s in comp)


def test_quiz_answer_keys(normal):
    qs = {q.kind: q for q in make_questions(for_experiment(normal))}

    def ans(q):
        return next(o["text"] for o in q.options if o["id"] == q.answer)

    assert ans(qs["basis"]) == "410개"
    assert ans(qs["diff"]) == "60개 늘어난다"  # 79 − 19
    assert ans(qs["stockout"]) == "80%만"  # 80%만 잔여 0개
    assert ans(qs["before"]) == "5%"
    for q in qs.values():
        assert len({o["text"] for o in q.options}) == len(q.options) >= 3


def test_nested_result_metadata_drives_both_input_and_filled_values(normal):
    from schemas.base import RiskBasis

    # 실제 엔진이 추가 메타데이터를 ProductResult 안에만 보내는 경우.
    case = normal.model_copy(
        update={
            "method_fallback": True,
            "risk_basis": "SKU",
            "no_date_receipt_qty": 0,
            "result": normal.result.model_copy(
                update={
                    "method_fallback": False,
                    "risk_basis": RiskBasis.POOLED,
                    "no_date_receipt_qty": 5,
                }
            ),
        }
    )
    inp = to_input(case)
    assert inp is not None and inp.has_no_date_receipts and not inp.method_fallback
    values = all_values(case)
    assert values["no_date_receipts"] == "5"
    assert values["risk_basis"] == "전체 상품의 과거 오차 기준"
    assert not case.context.method_fallback


def test_service_valid_no_date_draft_is_not_an_evaluation_insert_error(normal):
    from insight.evaluation.bank import generate
    from insight.service import generate_insight
    from schemas.base import RiskBasis
    from schemas.insight import Validation

    case = normal.model_copy(
        update={
            "no_date_receipt_qty": 0,
            "risk_basis": "SKU",
            "result": normal.result.model_copy(update={"risk_basis": RiskBasis.POOLED, "no_date_receipt_qty": 5}),
        }
    )
    inp = to_input(case)
    prompt = build_prompt(inp)
    draft = json.loads(StubClient(bad_rate=0).complete(prompt.system, prompt.user).text)
    draft["sentences"][-1] = (
        "도착 예정일 없는 발주 {{no_date_receipts}}개는 계산에 넣지 않았으며, " + draft["sentences"][-1]
    )
    raw = json.dumps(draft, ensure_ascii=False)
    assert generate_insight(inp, FakeClient([raw])).validation == Validation.PASSED
    result = generate(case, FakeClient([raw]))
    assert result.path == "llm" and result.number_check_ok
    assert result.reason_code is None
    assert "도착 예정일 없는 발주 5개" in result.text


def test_missing_observed_tokens_keep_report_totals_unknown():
    from insight.evaluation.bank import GenResult, offline_report
    output = GenResult("fixture", "llm", "text", None, None, None, None, None, True, True,
                       input_tokens=None, output_tokens=None)
    report = offline_report([output], CFG, 1)
    assert report["tokens"] == {"input_total": None, "output_total": None}
    assert report["cost_usd"] is None
