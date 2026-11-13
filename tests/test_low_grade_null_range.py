"""Client81: DA07의 짧은 이력/범위 미산출을 provider stub까지 재현한다. 실 LLM 호출 없음."""
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from engine.calculate import calculate
from engine.insight_facts import to_insight_input
from engine.risk import RiskContext
from insight.llm import LLMResult
from insight.prompt import InsightInputError, build_prompt
from insight.service import generate_insight
from schemas.base import RiskStatus
from schemas.insight import Validation
from tests.test_calculate import request_for
from tests.test_insight_facts import load_result


def test_twelve_day_engine_result_to_provider_and_validator():
    today = date(2026, 11, 13)
    request = request_for(today, today, horizon=12, lead=3, receipts=[(today + timedelta(days=1), 4)])
    request.settings[0].unit_cost = 100
    frame = pd.DataFrame([
        {"product_id": 101, "upload_file_id": 21, "stock_date": today - timedelta(days=k),
         "outbound_qty": 2, "closing_qty": 4, "is_valid_observation": True}
        for k in range(12)
    ])
    context = RiskContext((), date(2026, 5, 1), date(2026, 7, 31), 20)
    result = calculate(request, frame, risk_context=context).results[0]
    assert result.valid_observation_days == 12
    assert result.recommended_qty == 16 and result.recommended_amount == 1600
    assert result.accuracy_grade.value == "LOW" and result.risk_status == RiskStatus.LOW_GRADE
    assert result.forecast_range_low is result.forecast_range_high is result.pre_arrival_risk is None
    before = result.model_dump()
    inp = to_insight_input(result, "이 상품", as_of_date=today)
    assert inp is not None
    assert result.model_dump() == before
    prompt = build_prompt(inp)
    assert not any(k.startswith(("risk_", "forecast_range_")) for k in prompt.allowed)
    assert prompt.required == ["qty_p100"]
    assert "범위도 산출하지 않았습니다" in prompt.user
    class ProviderStub:
        calls = 0
        def complete(self, system, user):
            self.calls += 1
            assert user == prompt.user and system == prompt.system
            assert "16" not in user and "160" not in user
            return LLMResult(json.dumps({"sentences": [
                "{{qty_p100}}개를 넣으면 다음 입고 때 {{ending_p100}}개가 남습니다.",
                "예측 기록이 짧아 품절 위험과 예상 출고량 범위는 산출하지 않았습니다.",
            ]}, ensure_ascii=False), "offline-stub", 0, 0, 1, 0, False)
    client = ProviderStub()
    response = generate_insight(inp, client)
    assert client.calls == 1 and response.validation == Validation.PASSED
    assert "forecast_range" not in response.draft and "risk_post" not in response.draft


def test_low_grade_null_range_rejects_invented_range_and_raw_probability():
    result, _ = load_result("low_grade")
    result.forecast_range_low = result.forecast_range_high = None
    inp = to_insight_input(result, "이 상품")
    assert inp is not None
    class BadProvider:
        def complete(self, system, user):
            return LLMResult(json.dumps({"sentences": [
                "권장량은 {{qty_p100}}개입니다.",
                "범위는 {{forecast_range_low}}개이고 품절 위험은 10%입니다.",
            ]}), "offline-stub", 0, 0, 1, 0, False)
    response = generate_insight(inp, BadProvider())
    assert response.validation == Validation.FAILED and response.draft is None


@pytest.mark.parametrize("field,value", [("forecast_range_low", -1), ("forecast_range_high", -1),
                                        ("forecast_range_low", float("inf"))])
def test_invalid_ranges_remain_unavailable(field, value):
    result, _ = load_result("low_grade")
    setattr(result, field, value)
    assert to_insight_input(result, "이 상품") is None


def test_partial_range_input_is_rejected_before_provider():
    result, _ = load_result("low_grade")
    inp = to_insight_input(result, "이 상품")
    inp.placeholders.remove("forecast_range_high")
    with pytest.raises(InsightInputError):
        build_prompt(inp)
