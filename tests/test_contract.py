"""계약 테스트: 요청 예시와 mock 응답이 스키마를 통과하는지 확인한다."""
import pytest
from pydantic import ValidationError

from app.mocks import load_mock
from schemas.base import ResultStatus
from schemas.engine import (
    BacktestRequest,
    BacktestResponse,
    CalculateRequest,
    CalculateResponse,
    CleanRequest,
    CleanResponse,
    ProductResult,
    ScenarioRequest,
    ScenarioResult,
)
from schemas.insight import InsightRequest, InsightResponse
from tests.payloads import (
    BACKTEST_REQUEST,
    CALC_REQUEST,
    CLEAN_REQUEST,
    INSIGHT_REQUEST,
    SCENARIO_REQUEST,
)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CleanRequest, CLEAN_REQUEST),
        (CalculateRequest, CALC_REQUEST),
        (ScenarioRequest, SCENARIO_REQUEST),
        (BacktestRequest, BACKTEST_REQUEST),
        (InsightRequest, INSIGHT_REQUEST),
    ],
)
def test_request_examples_match_schema(model, payload):
    model.model_validate(payload)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("clean", CleanResponse),
        ("calculate", CalculateResponse),
        ("scenarios", ScenarioResult),
        ("backtest", BacktestResponse),
        ("insight", InsightResponse),
    ],
)
def test_mock_responses_match_schema(name, model):
    load_mock(name, model)


def test_json_uses_camel_case():
    out = load_mock("calculate", CalculateResponse).to_json_dict()
    assert out["results"][0]["requiredQtyBeforeRounding"] == 66.66
    assert "required_qty_before_rounding" not in out["results"][0]


def test_not_calculable_keeps_reason_not_zero():
    r = ProductResult(product_id=121, status=ResultStatus.NOT_CALCULABLE,
                      reason="판매한 날(유효 관측일)이 7일보다 적어요")
    out = r.to_json_dict()
    assert out["recommendedQty"] is None  # 0개로 바꾸지 않는다
    assert out["reason"]


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        CalculateRequest.model_validate({**CALC_REQUEST, "leadTimeDay": 5})


def test_clean_request_needs_at_least_one_file():
    with pytest.raises(ValidationError):
        CleanRequest.model_validate({**CLEAN_REQUEST, "files": []})


def test_not_calculable_without_reason_is_rejected():
    with pytest.raises(ValidationError):
        ProductResult(product_id=121, status=ResultStatus.NOT_CALCULABLE)
