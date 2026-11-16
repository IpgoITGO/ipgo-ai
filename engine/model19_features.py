# Source: IpgoITGO/model 66bffa0f0729a0365244c055bd9c4aeed830a649
"""기존 예측 실험에서 재사용하는 특징 생성·분위수 후처리. 수식은 원본과 동일."""
import numpy as np
import pandas as pd

HORIZON = 19
MIN_TRAIN_SAMPLES = 5
FEATURES = ["product_id", "month_sin", "month_cos", "weekday", "valid_history",
            "days_since_outbound"] + [f"{stat}_{window}" for window in (7,28,90)
            for stat in ("mean","std","max","positive_rate","valid_days")]
FEATURES += ["mean7_to_28", "mean28_to_90"]


def features_and_labels(sku):
    """특징은 당일까지, 목표는 다음 19일 전체가 유효할 때만 생성한다."""
    rows=sku.copy().set_index(pd.to_datetime(sku.stock_date)).sort_index()
    rows=rows.reindex(pd.date_range(rows.index.min(),rows.index.max(),freq="D"))
    valid=rows.is_valid_observation.fillna(False).astype(bool)
    quantity=rows.outbound_qty.where(valid)
    f=pd.DataFrame(index=rows.index)
    f["product_id"]=int(sku.product_id.iloc[0])
    f["month_sin"]=np.sin(2*np.pi*rows.index.month/12)
    f["month_cos"]=np.cos(2*np.pi*rows.index.month/12)
    f["weekday"]=rows.index.dayofweek
    f["valid_history"]=valid.cumsum()
    last_sale=pd.Series(rows.index,index=rows.index).where(quantity>0).ffill()
    f["days_since_outbound"]=(pd.Series(rows.index,index=rows.index)-last_sale).dt.days
    for window in (7,28,90):
        rolling=quantity.rolling(window,min_periods=1)
        f[f"mean_{window}"]=rolling.mean()
        f[f"std_{window}"]=rolling.std(ddof=0)
        f[f"max_{window}"]=rolling.max()
        count=rolling.count()
        f[f"valid_days_{window}"]=count
        positive=(quantity>0).astype(float).where(valid).rolling(window,min_periods=1).sum()
        f[f"positive_rate_{window}"]=positive/count.replace(0,np.nan)
    f["mean7_to_28"]=f.mean_7/f.mean_28.replace(0,np.nan)
    f["mean28_to_90"]=f.mean_28/f.mean_90.replace(0,np.nan)
    # 미관측을 0으로 채우면 품절을 낮은 수요로 학습하므로 해당 목표 전체를 제외한다.
    f["actual"]=quantity.shift(-1).iloc[::-1].rolling(HORIZON,min_periods=HORIZON).sum().iloc[::-1]
    f["baseline_forecast"]=f.mean_28*HORIZON
    f["origin"]=f.index.date
    f["target_end"]=(f.index+pd.Timedelta(days=HORIZON)).date
    return f


def matrix(rows):
    x=rows[FEATURES].copy()
    # 상품 ID는 수치 크기가 아니라 범주이다. 코드값은 파일명 정렬 기준으로 고정한다.
    x["product_id"]=x.product_id.astype("int32")
    return x


def postprocess(raw, rows, counts):
    # 분위수 교차를 정렬로 보정하고 학습 이력이 부족한 품목은 기존 B를 사용한다.
    values=np.sort(np.maximum(raw,0.),axis=1)
    fallback=rows.product_id.map(counts).fillna(0).to_numpy()<MIN_TRAIN_SAMPLES
    values[fallback,:]=rows.baseline_forecast.to_numpy()[fallback,None]
    assert np.isfinite(values).all() and (values>=0).all()
    return values,fallback
