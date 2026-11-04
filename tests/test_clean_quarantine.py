"""추정 정정 없이 제외하며 다음 월 재고 연결에 필요한 관측 기록을 보존한다."""
from datetime import date

import pytest

from engine.clean import CleaningInputError, clean, read_ledger
from schemas.engine import SourceFile
from tests.ledger_xlsx import build_ledger


def test_pre_receipt_quarantine_keeps_grid_and_post_receipt_provenance():
    raw = build_ledger([1,2,3], [dict(barcode='bad',name='검토상품',cost=1,mixed_id='bad',
        received_on='2026-05-02',records={1:(0,1,0,9),2:(0,1,0,8),3:(0,1,0,7)}),
        dict(barcode='good',name='정상상품',cost=1,mixed_id='good',received_on='2026-05-01',
             records={1:(0,1,0,9),2:(0,1,0,8),3:(0,1,0,7)})])
    with pytest.raises(CleaningInputError):
        read_ledger(raw,'2026-05.xlsx')
    source=SourceFile(upload_file_id=1,storage_key='fake',original_name='2026-05.xlsx')
    result=clean([(source,raw)],set())
    assert {e.sku_barcode for e in result.validation.excluded_skus} == {'bad'}
    assert result.validation.target_sku_count == result.validation.checked_sku_count-1
    bad=[r for r in result.product_daily_stocks if r.sku_barcode=='bad']
    assert len(bad) == len([r for r in result.product_daily_stocks if r.sku_barcode=='good'])
    assert not any(r.is_valid_observation for r in bad)
    records=[r for r in result.unit_daily_records if r.source_inventory_key=='bad']
    assert all(r.stock_date >= r.received_on and r.source_row==5 for r in records)
    assert any(r.stock_date==date(2026,5,3) for r in records)
    assert '5행' in result.validation.excluded_skus[0].reason
