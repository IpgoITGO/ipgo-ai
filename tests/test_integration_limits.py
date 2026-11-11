"""연결 경계와 용량 제한은 합성 입력과 가짜 DB/SQS만 사용한다."""
import json
import threading
from datetime import date, timedelta
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import pytest

from app import repository
from app.execution import engine_slot, validate_calculation_capacity
from app.worker import process_message, run_loop
from engine import clean as cleaning
from engine.calculate import CalculationInputError
from schemas.engine import CalculateRequest, SourceFile
from tests.ledger_xlsx import build_ledger
from tests.payloads import CALC_REQUEST, CLEAN_REQUEST


def unit(**changes):
    return {"barcode": "A", "name": "상품", "cost": 10, "mixed_id": "A",
            "received_on": "2026-05-01", "records": {1: (10, 1, 0, 9)}, **changes}


def source():
    return SourceFile(uploadFileId=21, storageKey="fake", originalName="2026-05.xlsx")


def test_clean_rejects_ambiguous_cost_and_date():
    with pytest.raises(cleaning.CleaningInputError):
        cleaning.clean([(source(), build_ledger([1], [unit(), unit(mixed_id="B", cost=11)]))], set())
    quarantined = cleaning.clean([(source(), build_ledger([1], [unit(received_on="2026-05-02")]))], set())
    assert quarantined.validation.excluded_skus
    assert not any(r.is_valid_observation for r in quarantined.product_daily_stocks)
    assert not quarantined.unit_daily_records


def test_zip_and_forged_dimension_limits(monkeypatch):
    content = build_ledger([1], [unit(), unit(barcode="B", mixed_id="B")])
    monkeypatch.setattr(cleaning, "MAX_UNCOMPRESSED", 1)
    with pytest.raises(cleaning.CleaningInputError):
        cleaning.clean([(source(), content)], set())
    monkeypatch.setattr(cleaning, "MAX_UNCOMPRESSED", 64 * 1024 * 1024)
    monkeypatch.setattr(cleaning, "MAX_SOURCE_ROWS", 1)
    edited = BytesIO()
    with ZipFile(BytesIO(content)) as src, ZipFile(edited, "w", ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                assert b'<dimension ref="A1:U6"' in data
                data = data.replace(b'<dimension ref="A1:U6"', b'<dimension ref="A1:A1"')
            dst.writestr(info.filename, data)
    with pytest.raises(cleaning.CleaningInputError, match="행 수"):
        cleaning.clean([(source(), edited.getvalue())], set())


def test_capacity_boundaries():
    req = CalculateRequest.model_validate(CALC_REQUEST)
    req.today = req.as_of_date + timedelta(days=730)
    req.settings[0].lead_time_days = 1
    req.settings[0].review_cycle_days = 365
    validate_calculation_capacity(req)
    req.today += timedelta(days=1)
    with pytest.raises(CalculationInputError):
        validate_calculation_capacity(req)
    req.today = req.as_of_date
    req.settings[0].review_cycle_days = 366
    with pytest.raises(CalculationInputError):
        validate_calculation_capacity(req)
    req.settings[0].review_cycle_days = 365
    req.product_ids = None
    req.settings = [req.settings[0].model_copy(update={"product_id": n}) for n in range(55)]
    with pytest.raises(CalculationInputError):
        validate_calculation_capacity(req)


@pytest.mark.parametrize(("products", "days", "allowed"), [
    (284, 19, True),  # #96: 284 × 3 × 19 = 16,188
    (284, 12, True),
    (300, 66, True),  # 59,400
    (999, 20, True),
    (1000, 20, True),  # 60,000까지 허용
    (1001, 20, False),
])
def test_calculation_scenario_capacity(products, days, allowed):
    req = CalculateRequest.model_validate(CALC_REQUEST)
    req.product_ids = None
    req.settings = [req.settings[0].model_copy(update={
        "product_id": n, "lead_time_days": 5, "review_cycle_days": days - 5,
    }) for n in range(products)]
    if allowed:
        validate_calculation_capacity(req)
    else:
        with pytest.raises(CalculationInputError, match="시나리오"):
            validate_calculation_capacity(req)


def test_capacity_counts_only_selected_products():
    req = CalculateRequest.model_validate(CALC_REQUEST)
    req.settings = [req.settings[0].model_copy(update={
        "product_id": n, "lead_time_days": 5, "review_cycle_days": 15,
    }) for n in range(1001)]
    req.product_ids = list(range(1000))
    validate_calculation_capacity(req)
    req.product_ids.append(1000)
    with pytest.raises(CalculationInputError, match="시나리오"):
        validate_calculation_capacity(req)


def test_expanded_capacity_calculates_and_transports_results(monkeypatch):
    from app import jobs
    from app.result_transport import MAX_RESULT_BYTES, encode_result

    # 하루 출고 1개, 시작 재고 9개: 66일 필요량 66 - 9 = 57개.
    products = range(1, 301)
    daily = pd.DataFrame([{
        "product_id": product, "upload_file_id": 1, "stock_date": date(2026, 5, day),
        "outbound_qty": 1, "closing_qty": 9, "is_valid_observation": True,
    } for product in products for day in range(1, 32)])
    monkeypatch.setattr(jobs.repository, "load_product_daily", lambda *_args, **_kwargs: daily)
    request = CalculateRequest.model_validate({
        "runId": 1, "userId": 1, "warehouseId": 1, "uploadFileIds": [1],
        "asOfDate": "2026-05-31", "today": "2026-05-31", "policy": {"defaultMethod": "B"},
        "settings": [{"productId": product, "leadTimeDays": 5, "reviewCycleDays": 61,
                      "orderUnitQty": 1, "unitCost": 10} for product in products],
    })
    result = jobs.execute(jobs.JobEnvelope(
        contract_version="1", job_id="capacity-test", job_type=jobs.JobType.CALCULATE,
        payload=request.to_json_dict(),
    ))
    assert result.status == jobs.JobStatus.SUCCEEDED
    assert len(result.result["results"]) == 300
    assert all(row["status"] == "OK" and row["recommendedQty"] == 57
               and len(row["scenarios"]) == 3
               and all(len(scenario["dailyProjection"]) == 66 for scenario in row["scenarios"])
               for row in result.result["results"])
    assert len(encode_result(result).encode()) <= MAX_RESULT_BYTES


def test_busy_http_and_worker_leave_message(client, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "real")
    with engine_slot():
        response = client.post("/engine/calculate", json=CALC_REQUEST)
        assert response.status_code == 503 and response.json()["code"] == "ENGINE_BUSY"
        assert client.get("/health").status_code == 200
        with pytest.raises(Exception) as error:
            process_message(json.dumps({"contractVersion": "1", "jobId": "1", "jobType": "CLEAN",
                                        "payload": CLEAN_REQUEST}))
        assert error.value.code == "ENGINE_BUSY"


def test_worker_invalid_source_and_receive_retry(monkeypatch):
    body = {"contractVersion": "1", "jobId": "1", "jobType": "CLEAN", "payload": CLEAN_REQUEST}
    monkeypatch.setattr("app.jobs.repository.load_cleaning_context", lambda *_: (set(), {}, None))
    monkeypatch.setattr("app.jobs.storage.read_source_file", lambda _: b"bad xlsx")
    result = process_message(json.dumps(body))
    assert result.error.code == "INVALID_SOURCE"
    body["payload"] = {**CLEAN_REQUEST, "files": []}
    assert process_message(json.dumps(body)).error.code == "INVALID_SOURCE"
    stop = threading.Event()

    class Queue:
        calls = 0

        def receive_message(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("secret")
            stop.set()
            return {}

    queue = Queue()
    monkeypatch.setattr(stop, "wait", lambda _seconds: False)
    run_loop(stop, SimpleNamespace(jobs_queue_url="fake"), queue)
    assert queue.calls == 2


@pytest.mark.parametrize("source_rows", [[(21, 2, 3, "DONE")], [(21, 1, 4, "DONE")],
                                        [(21, 1, 3, "PROCESSING")], []])
def test_ownership_failure_rolls_back(monkeypatch, source_rows):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, *_args): pass
        def fetchall(self): return source_rows

    class Connection:
        rolled_back = False
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def cursor(self): return Cursor()
        def rollback(self): self.rolled_back = True

    connection = Connection()
    monkeypatch.setattr(repository, "get_settings", lambda: SimpleNamespace(database_url="fake"))
    monkeypatch.setattr(repository.psycopg, "connect", lambda *_a, **_kw: connection)
    with pytest.raises(CalculationInputError):
        repository.load_product_daily(3, [21], user_id=1)
    assert connection.read_only and connection.rolled_back


def test_fake_xlsx_flows_into_actual_calculation(monkeypatch):
    from app import jobs
    from schemas.engine import CleanRequest

    records = {d: (20 if d == 1 else 0, 1, 0, 20 - d) for d in range(1, 9)}
    monkeypatch.setattr(repository, "load_cleaning_context", lambda *_: (set(), {}, None))
    monkeypatch.setattr(jobs.storage, "read_source_file", lambda _: build_ledger(list(range(1, 9)),
                                                                                [unit(records=records)]))
    cleaned = jobs.clean_request(CleanRequest.model_validate(
        {**CLEAN_REQUEST, "files": [{**CLEAN_REQUEST["files"][0], "originalName": "2026-05.xlsx"}]}))
    assert cleaned.validation.checked_sku_count == 1 and cleaned.validation.target_sku_count == 1
    import pandas as pd

    daily = pd.DataFrame([{"product_id": 101, "upload_file_id": 21, "stock_date": row.stock_date,
                           "outbound_qty": row.outbound_qty, "closing_qty": row.closing_qty,
                           "is_valid_observation": row.is_valid_observation}
                          for row in cleaned.product_daily_stocks])
    seen = []

    def read(_warehouse_id, ids, *, user_id):
        seen.append((ids, user_id))
        return daily

    monkeypatch.setattr(repository, "load_product_daily", read)
    payload = {**CALC_REQUEST, "uploadFileIds": [21], "asOfDate": "2026-05-08", "today": "2026-05-08",
               "plannedReceipts": []}
    result = jobs.execute(jobs.JobEnvelope(contractVersion="1", jobId="1", jobType="CALCULATE", payload=payload))
    assert seen == [([21], 1)]
    assert result.result["results"][0]["productId"] == 101
    assert result.result["results"][0]["validObservationDays"] == 7


def test_previous_month_mismatch_says_how_many_units_and_carries_a_reason_code():
    # #94: 전월에 재고가 남은 재고관리단위가 이번 파일에 없으면 개수와 구분 코드를 같이 알려 준다.
    previous = {("GONE", date(2026, 4, 20)): 5, ("ZERO", date(2026, 4, 20)): 0}
    with pytest.raises(cleaning.CleaningInputError) as raised:
        cleaning.clean([(source(), build_ledger([1], [unit()]))], set(), previous_units=previous)
    assert "전월 잔여 재고의 재고관리단위가 이번 자료에 없어요(1개)" in str(raised.value)
    assert raised.value.reason == "PREVIOUS_MONTH_MISMATCH"


def test_worker_sends_the_cleaning_sentence_and_reason_to_the_server(monkeypatch):
    # #94: worker가 정제 오류 문장을 "원본을 확인해 주세요."로 바꾸지 않고 그대로 보낸다.
    def fail(_job):
        raise cleaning.CleaningInputError("같은 바코드에 서로 다른 원가가 있어요")
    monkeypatch.setattr("app.worker.execute", fail)
    body = json.dumps({"contractVersion": "1", "jobId": "clean-94", "jobType": "CLEAN", "payload": CLEAN_REQUEST})
    result = process_message(body)
    assert result.error.code == "INVALID_SOURCE"
    assert result.error.message == "같은 바코드에 서로 다른 원가가 있어요"
    assert result.error.details is None

    def mismatch(_job):
        raise cleaning.CleaningInputError("전월 잔여 재고…(2개)", reason="PREVIOUS_MONTH_MISMATCH")
    monkeypatch.setattr("app.worker.execute", mismatch)
    wire = json.loads(process_message(body).model_dump_json(by_alias=True))
    assert wire["error"] == {"code": "INVALID_SOURCE", "message": "전월 잔여 재고…(2개)",
                             "details": {"reason": "PREVIOUS_MONTH_MISMATCH"}}
