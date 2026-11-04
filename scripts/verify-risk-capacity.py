"""31상품 합성 시계열로 기본 DA07 정책의 공통 계산 경로를 검사한다. 실제 DB/LLM은 쓰지 않는다."""
import json
import resource
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app import repository
from app.jobs import JobEnvelope
from app.worker import process_message
from schemas.engine import CalculateRequest

today = date(2026, 11, 3)
dates = []
for start, end in [(date(2025, 2, 1), date(2025, 7, 31)), (date(2026, 2, 1), today)]:
    dates.extend(start + timedelta(days=n) for n in range((end - start).days + 1))
daily = pd.DataFrame([
    {"product_id": sku, "upload_file_id": 1, "stock_date": day,
     "outbound_qty": 1, "closing_qty": 9, "is_valid_observation": True}
    for sku in range(1, 32) for day in dates
])
repository.load_product_daily = lambda *_args, **_kwargs: daily
request = CalculateRequest(
    run_id=1, user_id=1, warehouse_id=1, upload_file_ids=[1], as_of_date=today, today=today,
    product_ids=list(range(1, 32)), policy={"defaultMethod": "B"},
    settings=[{"productId": n, "leadTimeDays": 5, "reviewCycleDays": 29,
               "orderUnitQty": 1, "unitCost": 10} for n in range(1, 32)],
)
job = JobEnvelope(contract_version="1", job_id="synthetic-risk-capacity", job_type="CALCULATE",
                  payload=request.to_json_dict())
start = time.perf_counter()
result = process_message(job.model_dump_json(by_alias=True))
assert result.status == "SUCCEEDED"
rows = result.result["results"]
assert len(rows) == 31
assert all(r["riskStatus"] == "OK" and r["riskBasis"] == "POOLED" and r["riskSampleSize"] >= 20 for r in rows)
assert all(r["insightInput"] is not None and all(s["postArrivalRisk"] is not None for s in r["scenarios"])
           for r in rows)
metrics = {
    "scope": "단일 합성 요청·DB 조회 대체. 네트워크·실제 고객 흐름·LLM 검증 아님",
    "engine_version": result.result["engineVersion"], "products": len(rows), "source_rows": len(daily),
    "seconds": round(time.perf_counter() - start, 3),
    "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
}
for name in ("memory.current", "memory.peak", "memory.events"):
    path = Path("/sys/fs/cgroup") / name
    if path.exists():
        metrics[name] = path.read_text().strip()
print(json.dumps(metrics))

