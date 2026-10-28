"""테스트용 요청 예시 (API 명세서 4장 + 10/27 합의 필드)."""

CALC_REQUEST = {
    "runId": 34, "userId": 1, "warehouseId": 3, "uploadFileIds": [21, 22],
    "asOfDate": "2026-10-14", "today": "2026-10-19", "productIds": [101],
    "policy": {"defaultMethod": "A", "multiplier": 1.05},
    "settings": [{"productId": 101, "leadTimeDays": 5, "reviewCycleDays": 29, "orderUnitQty": 12, "unitCost": 1800}],
    "plannedReceipts": [{"productId": 101, "quantity": 120, "expectedOn": "2026-12-01"}],
}

CLEAN_REQUEST = {
    "uploadId": 15, "userId": 1, "warehouseId": 3,
    "files": [
        {"uploadFileId": 21, "storageKey": "uploads/1/2026-09.xlsx", "originalName": "2026-09.xlsx"},
        {"uploadFileId": 22, "storageKey": "uploads/1/2026-10.xlsx", "originalName": "2026-10.xlsx"},
    ],
}

SCENARIO_REQUEST = {"resultId": 9001, "calculationInput": CALC_REQUEST, "qty": 84}

BACKTEST_REQUEST = {
    "userId": 1, "warehouseId": 3, "uploadFileIds": [1, 2, 3],
    "tunePeriod": {"start": "2026-05-01", "end": "2026-07-31"},
    "testPeriod": {"start": "2026-08-01", "end": "2026-10-14"},
}

INSIGHT_REQUEST = {
    "resultId": 9001,
    "input": {
        "skuLabel": "상품 A", "recommended": "P100",
        "placeholders": ["qty_p80", "qty_p100", "qty_p120", "ending_p100",
                         "risk_post_p80", "risk_post_p100", "risk_pre"],
        "directions": {"risk_post_p80_vs_p100": "높음", "ending_p120_vs_p100": "높음"},
        "riskStatus": "OK",
    },
}
