import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch):
    """기본은 mock 모드. 실제 모드 테스트는 monkeypatch로 ENGINE_MODE=real을 준다."""
    monkeypatch.setenv("ENGINE_MODE", "mock")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("STORAGE_BASE", raising=False)
    monkeypatch.delenv("IPGO_UPLOAD_BUCKET", raising=False)
    monkeypatch.delenv("IPGO_JOBS_QUEUE_URL", raising=False)
    monkeypatch.delenv("IPGO_RESULTS_QUEUE_URL", raising=False)
    return TestClient(app, raise_server_exceptions=False)
