"""Proposed service rule: dated receipts cannot cover earlier demand."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import repository
from app.jobs import JobEnvelope, execute
from engine.model19 import Prediction
from tests.test_model19 import inputs


@pytest.mark.parametrize(
    'stock,target,receipts,unit,qty,pre',
    [
        pytest.param(7, 19, [(6, 12)], 1, 0, 0, id='early'),
        pytest.param(7, 19, [(18, 12)], 1, 10, 0, id='late'),
        pytest.param(7, 19, [(18, 12)], 4, 12, 0, id='rounded'),
        pytest.param(7, 19, [(5, 12)], 1, 0, 0, id='same-day'),
        pytest.param(0, 19, [(5, 15), (19, 4)], 1, 0, 4, id='pre-arrival-only'),
        pytest.param(7, 25, [(6, 12)], 1, 6, 0, id='existing-buffer'),
        pytest.param(7, 19, [(6, 1), (12, 3), (18, 10)], 1, 6, 0, id='multiple'),
    ],
)
def test_model19_dated_receipts_http_and_worker(client, monkeypatch, stock, target, receipts, unit, qty, pre):
    original, daily = inputs()
    request, _ = inputs(
        settings=[dict(productId=7, leadTimeDays=5, reviewCycleDays=14, orderUnitQty=unit, unitCost=100)],
        plannedReceipts=[dict(productId=7, quantity=amount, expectedOn=original.today + timedelta(days=day))
                         for day, amount in receipts],
    )
    daily['closing_qty'] = float(stock)
    # P80 + buffer가 목표이며 모델의 P90 상하한 안에 있다.
    prediction = Prediction(19, 19, max(20, target), target - 19, target, False)
    bundle = SimpleNamespace(version='test-version', predict=lambda *args: prediction)
    monkeypatch.setattr('engine.model19.load_bundle', lambda *args: bundle)
    monkeypatch.setenv('ENGINE_MODE', 'real')
    monkeypatch.setattr(repository, 'load_product_daily', lambda *args, **kwargs: daily)
    payload = request.to_json_dict()
    http = client.post('/engine/calculate', json=payload)
    assert http.status_code == 200
    job = JobEnvelope(contractVersion='1', jobId='dated-receipts', jobType='CALCULATE', payload=payload)
    assert execute(job).result == http.json()
    assert http.json()['engineVersion'] == '0.7.0+lgbm19'
    result = http.json()['results'][0]
    assert result['modelVersion'] == 'test-version'
    assert result['recommendedQty'] == qty
    assert result['targetStockQty'] == target
    assert result['forecastOutboundQty'] == 19
    assert result['preArrivalUnmetQty'] == pre
    if qty:
        assert result['scenarios'][1]['unmetDemandQty'] == pre
    if stock == 7 and receipts == [(18, 12)]:
        assert result['expectedShortageOn'] == (request.today + timedelta(days=7)).isoformat()
