"""Synthetic, single-process capacity probe; DB and S3 are replaced, no LLM is called.

Each of 31 SKUs receives 10 units and consumes 1 on day 1, then receives and
consumes 1 each day. Closing stock is 9, demand is 1/day. With a 30-day horizon,
no planned receipts and unit size 1, the hand-calculated recommendation is 21.
This probe checks the tested sample; it does not prove every admitted input fits.
"""
import json
import resource
import time
from datetime import date
from pathlib import Path

import pandas as pd

from app import jobs, repository, storage
from schemas.engine import CalculateRequest, CleanRequest
from tests.ledger_xlsx import build_ledger

units = [{"barcode": str(n), "name": f"상품{n}", "cost": 10, "mixed_id": str(n),
          "received_on": "2026-05-01", "records": {d: (10 if d == 1 else 1, 1, 0, 9)
                                                    for d in range(1, 32)}} for n in range(31)]
content = build_ledger(list(range(1, 32)), units)
repository.load_cleaning_context = lambda *_: (set(), {}, None)
storage.read_source_file = lambda _: content
request = CleanRequest.model_validate({"uploadId": 1, "userId": 1, "warehouseId": 1,
            "files": [{"uploadFileId": 1, "storageKey": "fake", "originalName": "2026-05.xlsx"}]})
t = time.perf_counter()
with jobs.engine_slot():
    clean = jobs.clean_request(request)
clean_seconds = time.perf_counter() - t
rows = [{"product_id": int(x.sku_barcode) + 1, "upload_file_id": 1, "stock_date": x.stock_date,
         "outbound_qty": x.outbound_qty, "closing_qty": x.closing_qty,
         "is_valid_observation": x.is_valid_observation} for x in clean.product_daily_stocks]
repository.load_product_daily = lambda *_args, **_kw: pd.DataFrame(rows)
calc = CalculateRequest.model_validate({"runId": 1, "userId": 1, "warehouseId": 1, "uploadFileIds": [1],
    "asOfDate": date(2026, 5, 31), "today": date(2026, 5, 31), "productIds": list(range(1, 32)),
    "policy": {"defaultMethod": "B"}, "settings": [{"productId": n, "leadTimeDays": 5,
        "reviewCycleDays": 25, "orderUnitQty": 1, "unitCost": 10} for n in range(1, 32)]})
t = time.perf_counter()
result = jobs.execute(jobs.JobEnvelope(contractVersion="1", jobId="fake", jobType="CALCULATE",
                                       payload=calc.to_json_dict()))
assert len(clean.products) == 31 and clean.validation.target_sku_count == 31
assert len(result.result["results"]) == 31
assert all(r["status"] == "OK" and r["recommendedQty"] == 21 and len(r["scenarios"]) == 3
           for r in result.result["results"])
metrics = {"products": len(clean.products), "unit_records": len(clean.unit_daily_records),
           "excluded": len(clean.validation.excluded_skus), "calc_products": len(result.result["results"]),
           "clean_seconds": round(clean_seconds, 3), "calculate_seconds": round(time.perf_counter()-t, 3),
           "scope": "Synthetic single process, mocked source/DB; no real AWS/LLM",
           "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
for name in ("memory.current", "memory.peak", "memory.events"):
    path = Path('/sys/fs/cgroup') / name
    if path.exists():
        metrics[name] = path.read_text().strip()
print(json.dumps(metrics))
