"""테스트용 가짜 수불부 xlsx를 메모리에서 만든다. 칸 배치는 실제 풀필먼트 수불부와 같다 (#12).

1행 식별 열 이름 · 2행 'DD 일' · 3행 입고 · 출고 · 조정 · 재고 반복 · 4행 빈 줄 · 5행부터 재고관리단위 1행씩.
"""
from io import BytesIO

import openpyxl

ID_COLUMNS = [
    "SKU바코드", "고객상품코드", "상품정보", "유통기한", "입고일자", "로트번호", "제조일자", "원가",
    "판매예약채널명", "판매예약채널구분", "sku_mixed_id", "SKU명", "벤더상품코드", "벤더명",
    "workplace_code", "workplace_name",
]
KINDS = ["입고", "출고", "조정", "재고"]
BLANK = (None, None, None, None)


def build_ledger(days: list[int], units: list[dict]) -> bytes:
    """units 항목: barcode · name · cost · mixed_id · received_on · records({일: (입고, 출고, 조정, 재고)}).

    records에 없는 날은 빈칸이다.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append([None, *ID_COLUMNS, "생성일: 2026-10-14 13:00:00"])
    day_row = ["구분"] + [None] * len(ID_COLUMNS)
    kind_row = ["대상"] + [None] * len(ID_COLUMNS)
    for d in days:
        day_row += [f"{d:02d} 일", None, None, None]
        kind_row += KINDS
    ws.append(day_row)
    ws.append(kind_row)
    ws.append([None])
    for no, u in enumerate(units, 1):
        row = [
            no, u["barcode"], None, None, None, u["received_on"], "-", "-", u["cost"], None, None,
            u["mixed_id"], u["name"], None, "테스트벤더", "FC_TEST01", "TEST_창고",
        ]
        for d in days:
            row += list(u["records"].get(d, BLANK))
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
