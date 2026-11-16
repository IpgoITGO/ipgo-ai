"""#122: 실제 서비스와 오프라인 평가 모두 LGBM19 설명을 생성하지 않는다."""
import json
from types import SimpleNamespace

import pytest

from app import calculation
from engine.insight_facts import to_insight_input
from engine.model19 import Prediction
from insight.evaluation.bank import experiment_candidate, generate, prepare
from insight.evaluation.cases import EvalCase
from insight.evaluation.config import load_config
from insight.evaluation.table import render_table
from schemas.base import ResultStatus
from tests.insight_fallback import Context, build_fallback, placeholder_values
from tests.test_model19 import inputs


class NoLLM:
    def complete(self, *args, **kwargs):
        pytest.fail("LGBM19 설명 미생성 상태에서는 LLM을 호출하면 안 된다")


@pytest.fixture
def model_case(monkeypatch):
    request, daily = inputs()
    monkeypatch.setattr("engine.model19.load_bundle", lambda *args: SimpleNamespace(
        version="test-version", predict=lambda *args: Prediction(19, 24, 30, 4, 28, False),
    ))
    result = calculation.calculate(request, daily).results[0]
    return EvalCase(id="synthetic-model19", as_of_date=request.as_of_date, result=result)


@pytest.mark.parametrize("status", [ResultStatus.OK, ResultStatus.NOT_NEEDED])
def test_model_has_neither_llm_nor_ab_fallback(model_case, status):
    r = model_case.result.model_copy(update={"status": status})
    before = r.to_json_dict()
    assert r.insight_input is None
    assert to_insight_input(r, "상품 A", as_of_date=model_case.as_of_date) is None
    assert placeholder_values(r, Context()) == {}
    assert build_fallback(r) is None
    out = generate(model_case.model_copy(update={"result": r}), NoLLM())
    assert out.path == "none" and not out.called
    assert out.reason_code == "model_explanation_deferred"
    assert "LGBM19 설명 미생성" in out.reason_detail
    assert out.text is out.fixed_text is out.template is None
    assert r.to_json_dict() == before


def test_offline_table_does_not_invent_formula_or_risk_reason(model_case):
    table = render_table(model_case)
    facts = dict(table["facts"])
    assert facts["예측 방식"] == "고정19일 LightGBM"
    assert "적용 산식" not in facts and "배수" not in facts
    assert facts["커버 기간 예상 출고"] == "19개"
    assert facts["도착 전 품절 위험 (모든 시나리오 공통)"] == "미산출"
    assert all(row[-1] == "미산출" for row in table["rows"])
    assert "표본 부족" not in json.dumps(table, ensure_ascii=False)
    assert experiment_candidate(model_case) == (False, "LGBM19 설명 미생성 (#122)")


def test_prepare_records_deferred_model_without_calling_llm(model_case, tmp_path):
    bank = {"all_results": [model_case.model_dump(mode="json", by_alias=True)], "results": []}
    log = tmp_path / "calls.jsonl"
    prepared = prepare(bank, load_config(), NoLLM(), log)
    record = json.loads(log.read_text())
    assert record["path"] == "none" and record["called"] is False
    assert record["fixed_text"] is record["text"] is None
    assert prepared["items"] == {}
