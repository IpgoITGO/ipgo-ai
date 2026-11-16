"""#117/#121: 합성 bundle로 파일·메타데이터 검사와 비유한 추론 실패 경계를 검증한다.

Booster만 가짜이며 실제 Bundle 초기화·해시·오차은행 검사 코드를 실행한다.
가중치 호환성이나 실제 모델 성능을 검증한 결과가 아니다.
"""
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app import repository
from app.jobs import JobEnvelope, execute
from engine import model19_features
from engine.model19 import FILES, POLICY, Model19Error, load_bundle
from tests.test_model19 import inputs
from tests.test_model19_features import fixture_bundle


@pytest.fixture
def synthetic_bundle(tmp_path, monkeypatch):
    for q in (50, 80, 90):
        (tmp_path / f"q{q}.txt").write_text("synthetic model, not real weights")
    (tmp_path / "metadata.json").write_text(json.dumps({
        "cutoff": "2026-07-31", "max_training_target_end": "2026-07-30", "product_train_counts": {"1": 5},
    }))
    (tmp_path / "product-map.json").write_text(json.dumps({"SYNTHETIC": 1}))
    (tmp_path / "residuals.csv").write_text(
        "product_id,origin,target_end,actual,q80,error,group\n"
        "1,2026-05-01,2026-05-20,30,25,5,STEADY\n"
        "1,2026-05-21,2026-06-09,20,25,0,STEADY\n"
    )

    class Booster:
        def __init__(self, model_file):
            self.params = {"objective": "quantile", "alpha": int(Path(model_file).stem[1:]) / 100}

        def feature_name(self):
            return model19_features.FEATURES

    monkeypatch.setitem(sys.modules, "lightgbm", SimpleNamespace(Booster=Booster))

    def seal():
        manifest = dict(modelVersion="synthetic-v1", policy=POLICY,
                        leadTimeDays=5, reviewCycleDays=14, horizonDays=19,
                        sha256={name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in FILES})
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    seal()
    return tmp_path, seal, Booster


def test_valid_bundle_loads_once_and_builds_error_bank(synthetic_bundle):
    root, _, _ = synthetic_bundle
    first = load_bundle(str(root), "synthetic-v1")
    assert load_bundle(str(root), "synthetic-v1") is first
    assert first.version == "synthetic-v1" and first.mapping == {"SYNTHETIC": 1}
    assert first.pooled == pytest.approx(4.5)
    assert first.sku_errors[1] == (2, pytest.approx(4.5))


@pytest.mark.parametrize("name", FILES)
@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_missing_or_tampered_files_fail_closed(synthetic_bundle, name, mutation):
    root, _, _ = synthetic_bundle
    if mutation == "missing":
        (root / name).unlink()
    else:
        (root / name).write_bytes(b"changed after manifest")
    with pytest.raises(Model19Error):
        load_bundle(str(root), "synthetic-v1")


@pytest.mark.parametrize(("key", "value"), [
    ("modelVersion", "other"), ("policy", "other"),
    ("leadTimeDays", 6), ("reviewCycleDays", 13), ("horizonDays", 20),
])
def test_manifest_contract_is_enforced(synthetic_bundle, key, value):
    root, _, _ = synthetic_bundle
    manifest = json.loads((root / "manifest.json").read_text())
    manifest[key] = value
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(Model19Error):
        load_bundle(str(root), "synthetic-v1")


@pytest.mark.parametrize("mutation", ["future_training", "unknown_training_id", "duplicate_mapping"])
def test_metadata_is_validated_after_file_hashes(synthetic_bundle, mutation):
    root, seal, _ = synthetic_bundle
    path = root / ("product-map.json" if mutation == "duplicate_mapping" else "metadata.json")
    meta = json.loads(path.read_text())
    if mutation == "future_training":
        meta["max_training_target_end"] = "2026-08-01"
    elif mutation == "unknown_training_id":
        meta["product_train_counts"] = {"2": 5}
    else:
        meta["ANOTHER_SYNTHETIC"] = 1
    path.write_text(json.dumps(meta))
    seal()
    with pytest.raises(Model19Error):
        load_bundle(str(root), "synthetic-v1")


@pytest.mark.parametrize("mutation", ["overlap", "future", "wrong_horizon", "unknown_id", "negative", "nan",
                                    "wrong_error", "duplicate"])
def test_error_bank_rejects_invalid_or_overlapping_observations(synthetic_bundle, mutation):
    root, seal, _ = synthetic_bundle
    path = root / "residuals.csv"
    bank = pd.read_csv(path)
    if mutation == "overlap":
        bank.loc[1, ["origin", "target_end"]] = ["2026-05-19", "2026-06-07"]
    elif mutation == "future":
        bank.loc[1, ["origin", "target_end"]] = ["2026-07-20", "2026-08-08"]
    elif mutation == "wrong_horizon":
        bank.loc[1, "target_end"] = "2026-06-08"
    elif mutation == "unknown_id":
        bank.loc[1, "product_id"] = 2
    elif mutation == "negative":
        bank.loc[1, "actual"] = -1
    elif mutation == "nan":
        bank["actual"] = bank.actual.astype(float)
        bank.loc[1, "actual"] = np.nan
    elif mutation == "wrong_error":
        bank.loc[1, "error"] = 1
    else:
        bank = pd.concat([bank, bank.iloc[[0]]], ignore_index=True)
    bank.to_csv(path, index=False)
    seal()
    with pytest.raises(Model19Error):
        load_bundle(str(root), "synthetic-v1")


@pytest.mark.parametrize("mutation", ["features", "objective", "alpha"])
def test_model_features_and_quantiles_are_checked(synthetic_bundle, mutation, monkeypatch):
    root, _, booster = synthetic_bundle

    class WrongBooster(booster):
        def __init__(self, model_file):
            super().__init__(model_file)
            if mutation in {"objective", "alpha"}:
                self.params[mutation] = "regression" if mutation == "objective" else 0.1

        def feature_name(self):
            return ["wrong"] if mutation == "features" else super().feature_name()

    monkeypatch.setitem(sys.modules, "lightgbm", SimpleNamespace(Booster=WrongBooster))
    with pytest.raises(Model19Error):
        load_bundle(str(root), "synthetic-v1")


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize("count", [4, 5])
def test_nonfinite_prediction_is_model_error_even_before_fallback(value, count):
    bundle, daily, day, _ = fixture_bundle(count=count)
    bundle.models[0].value = value
    with pytest.raises(Model19Error, match="계산 결과가 유효하지"):
        bundle.predict(daily, "SYNTHETIC", day)


@pytest.mark.parametrize("same_shape", [True, False])
def test_prediction_must_have_one_row_for_the_requested_origin(same_shape):
    bundle, daily, day, _ = fixture_bundle()
    wrong = SimpleNamespace(predict=lambda *args, **kwargs: np.array([1., 2.]))
    bundle.models = [wrong] * 3 if same_shape else [wrong, *bundle.models[1:]]
    with pytest.raises(Model19Error, match="계산 결과가 유효하지"):
        bundle.predict(daily, "SYNTHETIC", day)


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
def test_bad_prediction_returns_not_calculable_in_http_and_worker(client, monkeypatch, value):
    bundle, _, _, _ = fixture_bundle()
    bundle.version = "test-version"
    bundle.models[0].value = value
    request, daily = inputs()
    monkeypatch.setenv("ENGINE_MODE", "real")
    monkeypatch.setattr("engine.model19.load_bundle", lambda *args: bundle)
    monkeypatch.setattr(repository, "load_product_daily", lambda *args, **kwargs: daily)
    http = client.post("/engine/calculate", json=request.to_json_dict())
    assert http.status_code == 200
    product = http.json()["results"][0]
    assert product["status"] == "NOT_CALCULABLE"
    assert product["recommendedQty"] is None and product["insightInput"] is None
    assert product["reason"] == "모델 계산 결과가 유효하지 않아요"
    job = JobEnvelope(contractVersion="1", jobId="synthetic-bad-model", jobType="CALCULATE",
                      payload=request.to_json_dict())
    assert execute(job).result == http.json()
