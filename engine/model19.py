"""고정 19일 모델 추론. 비공개 가중치/매핑/보정은 read-only bundle로 받는다."""
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from engine import model19_features as features

POLICY = "BUFFER100_FLOOR0"
METHOD = "LGBM19"
HORIZON = 19
FILES = ("q50.txt", "q80.txt", "q90.txt", "metadata.json", "product-map.json", "residuals.csv")


class Model19Error(ValueError):
    """원본/모델 값이나 경로를 사용자 오류에 넣지 않는다."""


@dataclass(frozen=True)
class Prediction:
    p50: float
    p80: float
    p90: float
    buffer: float
    target: float
    fallback: bool


class Bundle:
    def __init__(self, directory: str, version: str):
        import lightgbm as lgb

        root = Path(directory)
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest["modelVersion"] != version or manifest["policy"] != POLICY:
            raise Model19Error("모델 버전과 계산 요청이 일치하지 않아요")
        if (manifest["leadTimeDays"], manifest["reviewCycleDays"], manifest["horizonDays"]) != (5, 14, 19):
            raise Model19Error("모델의 고정 기간 설정이 올바르지 않아요")
        for name in FILES:
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != manifest["sha256"][name]:
                raise Model19Error("모델 파일 검증에 실패했어요")
        self.version = version
        self.mapping = json.loads((root / "product-map.json").read_text())
        if len(set(self.mapping.values())) != len(self.mapping):
            raise Model19Error("모델 상품 대응 정보가 중복되어 있어요")
        meta = json.loads((root / "metadata.json").read_text())
        self.cutoff = date.fromisoformat(meta["cutoff"])
        self.counts = {int(k): int(v) for k, v in meta["product_train_counts"].items()}
        if meta["max_training_target_end"] > meta["cutoff"]:
            raise Model19Error("모델 학습 기준일이 올바르지 않아요")
        self.models = [lgb.Booster(model_file=str(root / f"q{q}.txt")) for q in (50, 80, 90)]
        if any(m.feature_name() != features.FEATURES or m.params.get("objective") != "quantile"
               or abs(float(m.params["alpha"]) - q / 100) > 1e-12
               for m, q in zip(self.models, (50, 80, 90), strict=True)):
            raise Model19Error("모델 특징 정의가 일치하지 않아요")
        bank = pd.read_csv(root / "residuals.csv")
        bank["origin"] = pd.to_datetime(bank.origin).dt.date
        bank["target_end"] = pd.to_datetime(bank.target_end).dt.date
        known = set(self.mapping.values())
        if (bank.empty or not set(bank.product_id).issubset(known) or not set(self.counts).issubset(known)
                or bank.target_end.max() > self.cutoff or bank.duplicated(["product_id", "origin"]).any()
                or not all((e - s).days == 19 for s, e in zip(bank.origin, bank.target_end, strict=True))
                or not np.isfinite(bank[["actual", "q80", "error"]]).all().all()
                or (bank[["actual", "q80", "error"]] < 0).any().any()
                or not np.allclose(bank.error, np.maximum(bank.actual - bank.q80, 0), rtol=0, atol=1e-9)):
            raise Model19Error("과거 오차 보정 자료가 올바르지 않아요")
        for _, sku in bank.sort_values("origin").groupby("product_id"):
            if any(b < a for a, b in zip(sku.target_end.iloc[:-1], sku.origin.iloc[1:], strict=True)):
                raise Model19Error("과거 오차 보정 기간이 겹쳐요")
        self.pooled = float(bank.error.quantile(.9))
        self.group_errors = {g: float(s.error.quantile(.9)) if len(s) >= 20 else self.pooled
                             for g, s in bank.groupby("group")}
        self.sku_errors = {int(pid): (len(s), float(s.error.quantile(.9)))
                           for pid, s in bank.groupby("product_id")}

    def predict(self, daily: pd.DataFrame, barcode: str, origin: date) -> Prediction:
        if origin < self.cutoff:
            raise Model19Error("모델 학습 기준일보다 이전 날짜는 계산할 수 없어요")
        if barcode not in self.mapping:
            raise Model19Error("학습 상품 대응 정보가 없는 상품이에요")
        sku = daily.copy()
        sku = sku[sku.stock_date <= origin]
        if sku.empty:
            raise Model19Error("모델 계산에 필요한 과거 기록이 없어요")
        sku["product_id"] = self.mapping[barcode]
        # 연구의 >=100개 비판매 분리. DB 출고·기말재고는 수정하지 않는다.
        sku["outbound_qty"] = sku.outbound_qty.where(sku.outbound_qty < 100, 0.)
        if sku.stock_date.max() < origin:
            # 계산일까지 관측되지 않은 날짜는 0판매가 아니라 미관측이다.
            tail = {"product_id":self.mapping[barcode], "stock_date":origin,
                    "outbound_qty":np.nan, "is_valid_observation":False}
            sku = pd.concat([sku, pd.DataFrame([tail])], ignore_index=True)
        row = features.features_and_labels(sku).iloc[[-1]]
        if row.valid_days_28.iloc[0] < 7:
            raise Model19Error("최근 28일의 유효 기록이 7일보다 적어요")
        raw = np.column_stack([m.predict(features.matrix(row), num_threads=1) for m in self.models])
        values, fallback = features.postprocess(raw, row, self.counts)
        p50, p80, p90 = (float(x) for x in values[0])
        r = row.iloc[0]
        rate, cv = r.positive_rate_90, r.std_90 / max(r.mean_90, 1e-12)
        group = ("SHORT_HISTORY" if r.valid_days_90 < 60 else "DORMANT" if rate == 0
                 else "INTERMITTENT" if rate <= .1 else "STEADY" if cv <= 1 else "VARIABLE")
        count, local = self.sku_errors.get(self.mapping[barcode], (0, self.pooled))
        weight = count / (count + 10)
        buffer = weight * local + (1 - weight) * self.group_errors.get(group, self.pooled)
        target = min(1.2 * p90, max(.8 * p90, p80 + buffer))
        if not all(math.isfinite(x) and x >= 0 for x in (p50, p80, p90, buffer, target)):
            raise Model19Error("모델 계산 결과가 유효하지 않아요")
        return Prediction(p50, p80, p90, buffer, target, bool(fallback[0]))


@lru_cache(maxsize=2)
def load_bundle(directory: str, version: str) -> Bundle:
    """불변 디렉터리와 버전 조합만 재사용한다. 파일은 실행 중 덮어쓰지 않는다."""
    if not directory or not version:
        raise Model19Error("모델 파일과 버전이 설정되지 않았어요")
    try:
        return Bundle(directory, version)
    except Model19Error:
        raise
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        raise Model19Error("모델 패키지를 읽을 수 없어요. 연결 설정을 확인해 주세요") from exc
