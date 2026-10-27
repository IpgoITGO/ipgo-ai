"""API 뼈대 테스트: mock 응답, 오류 형식, 미정 항목(DB · 저장소)의 예외 처리."""
import pytest

from tests.payloads import (
    BACKTEST_REQUEST,
    CALC_REQUEST,
    CLEAN_REQUEST,
    INSIGHT_REQUEST,
    SCENARIO_REQUEST,
)

ENDPOINTS = [
    ("/engine/clean", CLEAN_REQUEST),
    ("/engine/calculate", CALC_REQUEST),
    ("/engine/scenarios", SCENARIO_REQUEST),
    ("/engine/backtest", BACKTEST_REQUEST),
    ("/engine/insight", INSIGHT_REQUEST),
]


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["mode"] == "mock"


@pytest.mark.parametrize(("path", "payload"), ENDPOINTS)
def test_mock_mode_returns_spec_example(client, path, payload):
    res = client.post(path, json=payload)
    assert res.status_code == 200, res.text


def test_mock_response_is_camel_case(client):
    body = client.post("/engine/calculate", json=CALC_REQUEST).json()
    assert body["results"][0]["recommendedQty"] == 72
    assert "engineVersion" in body


def test_invalid_input_uses_spec_error_format(client):
    res = client.post("/engine/calculate", json={**CALC_REQUEST, "leadTimeDay": 5})
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "INVALID_INPUT"
    assert body["details"]["errors"][0]["field"] == "leadTimeDay"


def test_real_mode_clean_without_storage_is_explicit_error(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    res = client.post("/engine/clean", json=CLEAN_REQUEST)
    assert res.status_code == 501
    assert res.json()["code"] == "STORAGE_NOT_CONFIGURED"


def test_real_mode_calculate_without_db_is_explicit_error(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    res = client.post("/engine/calculate", json=CALC_REQUEST)
    assert res.status_code == 501
    assert res.json()["code"] == "DATABASE_NOT_CONFIGURED"
    assert res.json()["details"]["uploadFileIds"] == [21, 22]


def test_unexpected_error_hides_internals(client, monkeypatch):
    import app.routers.engine as router

    def boom(*_):
        raise RuntimeError("secret internal detail")

    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(router.repository, "load_product_daily", boom)
    res = client.post("/engine/calculate", json=CALC_REQUEST)
    assert res.status_code == 500
    assert res.json()["code"] == "ENGINE_ERROR"
    assert "secret" not in res.text
