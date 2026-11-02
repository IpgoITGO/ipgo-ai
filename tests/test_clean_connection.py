from datetime import date
from types import SimpleNamespace

import pytest

from app import jobs, repository
from engine.clean import CleaningInputError
from tests.ledger_xlsx import build_ledger

PAYLOAD = {"uploadId": 7, "userId": 3, "warehouseId": 4,
           "files": [{"uploadFileId": 7, "storageKey": "fake", "originalName": "2026년 5월.xlsx"}],
           "previousUploadFileId": 6}
UNIT = {"barcode": "ABC", "name": "상품", "cost": 10, "mixed_id": "A", "received_on": "2026-04-01",
        "records": {1: (0, 2, 0, 8), 2: (0, 1, 0, 7)}}


def test_http_and_dispatch_actual_xlsx(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: build_ledger([1, 2], [UNIT]))
    monkeypatch.setattr(repository, "load_cleaning_context", lambda *_: (
        {"ABC"}, {("A", date(2026, 4, 1)): 10}, (date(2026, 5, 1), date(2026, 5, 31))))
    result = client.post("/engine/clean", json=PAYLOAD)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["periodStart"] == "2026-05-01" and body["asOfDate"] == "2026-05-02"
    assert body["products"][0]["isNew"] is False
    assert body["unitDailyRecords"][0]["openingQty"] == 10
    assert body["unitDailyRecords"][0]["sourceSheet"] == "Sheet1"
    assert body["unitDailyRecords"][0]["sourceRow"] == 5
    assert body["productDailyStocks"][1]["closingQty"] == 7
    assert body["validation"]["excludedSkus"] == []
    job = jobs.JobEnvelope(contractVersion="1", jobId="x", jobType="CLEAN", payload=PAYLOAD)
    assert jobs.execute(job).result == body
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: b"invalid")
    assert client.post("/engine/clean", json=PAYLOAD).json()["code"] == "INVALID_SOURCE"


def test_previous_mismatch_and_invalid_values(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(repository, "load_cleaning_context", lambda *_: (
        set(), {("A", date(2026, 4, 1)): 11}, (date(2026, 5, 1), date(2026, 5, 31))))
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: build_ledger([1, 2], [UNIT]))
    body = client.post("/engine/clean", json=PAYLOAD).json()
    assert body["products"][0]["isNew"] is True
    assert body["validation"]["excludedSkus"][0]["date"] == "2026-05-01"
    bad = {**UNIT, "records": {1: (-1, 0, 0, 0)}}
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: build_ledger([1], [bad]))
    assert client.post("/engine/clean", json=PAYLOAD).status_code == 422


class Cursor:
    def __init__(self, current, prior, units):
        self.current, self.prior, self.units = current, prior, units
        self.query = ""
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, query, params=None):
        self.query = query
        self.queries.append((query, params))

    def fetchone(self):
        return self.prior if "ORDER BY revision_no" in self.query else self.current

    def fetchall(self):
        return [("ABC",)] if "ai_cleaning_products_v1" in self.query else self.units


class Connection:
    def __init__(self, cursor):
        self.cur = cursor
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def cursor(self):
        return self.cur

    def rollback(self):
        self.rolled_back = True


def test_readonly_owner_latest_revision_contract(monkeypatch):
    source = SimpleNamespace(upload_file_id=7, storage_key="fake", original_name="2026년 5월.xlsx")
    current = (7, 3, 4, "fake", source.original_name, date(2026, 5, 1), date(2026, 5, 31), 2, "PROCESSING")
    prior = (6, 3, 4, "old", "old.xlsx", date(2026, 4, 1), date(2026, 4, 30), 3, "DONE")
    cursor = Cursor(current, prior, [("A", date(2026, 4, 1), date(2026, 4, 30), 10),
                                     ("A", date(2026, 4, 1), date(2026, 4, 29), 11)])
    connection = Connection(cursor)
    monkeypatch.setattr(repository, "get_settings", lambda: SimpleNamespace(database_url="fake"))
    monkeypatch.setattr(repository.psycopg, "connect", lambda *_a, **_kw: connection)
    known, previous, _ = repository.load_cleaning_context(3, 4, 7, 6, source)
    assert known == {"ABC"} and previous[("A", date(2026, 4, 1))] == 10
    assert connection.read_only and connection.rolled_back
    latest = next(q for q, _ in cursor.queries if "ORDER BY revision_no" in q)
    assert "status =" not in latest
    cursor.prior = (*prior[:-1], "FAILED")
    with pytest.raises(CleaningInputError):
        repository.load_cleaning_context(3, 4, 7, 6, source)
    cursor.current = (7, 99, *current[2:])
    with pytest.raises(CleaningInputError):
        repository.load_cleaning_context(3, 4, 7, None, source)


@pytest.mark.parametrize("units", [
    [{**UNIT, "records": {1: (2**53, 0, 0, 0)}}],
    [UNIT, UNIT],
    [UNIT, {**UNIT, "mixed_id": "B", "name": "다른 상품"}],
])
def test_rejects_unsafe_and_ambiguous_source(client, monkeypatch, units):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(repository, "load_cleaning_context", lambda *_: (set(), {}, None))
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: build_ledger([1, 2], units))
    assert client.post("/engine/clean", json=PAYLOAD).status_code == 422


def test_rejects_period_and_previous_gap(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: build_ledger([1, 2], [UNIT]))
    monkeypatch.setattr(repository, "load_cleaning_context", lambda *_: (
        set(), {}, (date(2026, 5, 2), date(2026, 5, 31))))
    assert client.post("/engine/clean", json=PAYLOAD).status_code == 422
    monkeypatch.setattr(repository, "load_cleaning_context", lambda *_: (
        set(), {("A", date(2026, 4, 1)): 10}, (date(2026, 5, 1), date(2026, 5, 31))))
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: build_ledger([2], [UNIT]))
    assert client.post("/engine/clean", json=PAYLOAD).status_code == 422


def test_checked_and_excluded_counts_follow_stock_balance(client, monkeypatch):
    # A: 기초 10 → 8 → 7, B: 기초 10 → 8, 다음 날 기말 6은 출고 1과 불일치.
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr(repository, "load_cleaning_context", lambda *_: (set(), {}, None))
    other = {**UNIT, "barcode": "DEF", "mixed_id": "B", "name": "상품 B",
             "records": {1: (0, 2, 0, 8), 2: (0, 1, 0, 6)}}
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: build_ledger([1, 2], [UNIT, other]))
    result = client.post("/engine/clean", json=PAYLOAD)
    assert result.status_code == 200
    report = result.json()["validation"]
    assert report["checkedSkuCount"] == 2
    assert report["targetSkuCount"] == 1
    assert [sku["skuBarcode"] for sku in report["excludedSkus"]] == ["DEF"]
