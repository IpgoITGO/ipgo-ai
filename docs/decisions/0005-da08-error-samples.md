# 0005. DA-08 최소 시계열 오차 표본

- 상태: #36 내부 표본 생성 구현 범위 (2026-11-03). 공개 API·계산 정책 결정은 아니다.
- 근거: #36의 DA08→DA07 착수 범위, 기존 `forecast_a`/`forecast_b`, `BacktestRequest`의 기간 경계,
  [0001](0001-engine-contract.md), 기존 사람이 작성한 DA03 손계산 표.
- 전체 정책 선정·위험/등급·검산 지원 담당 확정은 별도로 남긴다. AI가 새 손계산 fixture를 완료 상태로 만들지 않는다.

## 입력과 범위

`generate_error_samples(product_daily, *, origins, horizon_days, period, period_name,
observed_through, product_ids=None, methods=(A,B))`는 `ErrorSampleBatch`를 반환한다.

- DataFrame은 상품별 일별 기록이며 `product_id`, `stock_date`, `outbound_qty`, `is_valid_observation`을 사용한다.
  ID는 양의 정수, 날짜는 Python date다. 회원·창고·업로드 소유권/최신 revision 선택은 기존 app/repository 책임이다.
  같은 상품·날짜 두 행을 합산하거나 최신 행으로 임의 선택하지 않는다.
- origins는 중복 없는 명시적 기준일 목록이다. 간격을 자동 선정하지 않으며 결과 순서는 상품 ID·기준일·A/B 순서로 고정한다.
- horizon_days는 해당 호출에서 평가할 양의 정수 일수다. 상품별 길이가 다르면 호출자가 길이별로 나눠 호출한다.
  기본/대체 산식을 고르거나 lead time·정책·현재 날짜를 추측하지 않는다.
- Period는 요청의 명시적 start/end를 사용한다. 고정 달력/연도를 코드에 넣지 않는다.
  `origin >= period.start` 및 `origin+H <= period.end`인 창만 허용하고 끝을 잘라 평가하지 않는다.
- 관측 확정일 observed_through를 필수로 받는다. `origin+H <= observed_through`여야 평가한다.
  제공된 DataFrame에 미래 실적이 있어도 사용하지 않으며, 상품 목록 자동 도출도 확정일 이내 기록에서만 한다.
- 입력 DataFrame·기준일·상품/산식 목록을 수정하지 않는다. 표본/제외 결과는 frozen dataclass와 tuple이다.

## 예측·실적·오차

- 예측 함수에는 해당 SKU의 `stock_date <= origin` 사본만 전달한다. 작년/학습 기록은 평가 기간 시작 전이라도 필요할 수 있다.
  A/B의 기존 유효일/작년 창/가정은 바꾸지 않는다. A 불가를 B 결과로 바꿔 A 표본에 넣지 않는다.
- 실적은 `(origin, origin+H]`의 각 날짜가 한 번씩 있고 모든 is_valid_observation이 True일 때의 관측 출고 합계다.
  기록 누락을0으로 채우거나, 품절 등 무효 관측을 수요0으로 해석하지 않는다.
- 이는 DA02 유효 관측 표식에 따른 **관측 출고 오차**다. 잠재/실제 수요를 복원한 값은 아니다.
  DA02에서 유효로 표시한 기말 재고0일을 이 함수가 별도 기준으로 재분류하지 않는다.
- `error_qty = actual_qty - forecast_qty` (개), `absolute_error = abs(error_qty)` (개),
  `actual_to_forecast_ratio = actual_qty / forecast_qty` (무차원)다. 소수 예측값을 수량 단위로 올리지 않는다.
- 예측0은 부호/절대 오차 표본을 유지하고 비율만 None/ZERO_FORECAST다. 0/0을1로 만들지 않는다.
  비율 overflow도 None/NONFINITE_RATIO로 남긴다. 실제 출고0·양수 예측은 유효한 비율0이다.
- 비유한/음수 유효 관측, 유효 관측 표식 오류, 중복, 덜 지난 창, 기록 누락, 산식 사용 불가 등은
  SkippedSample에 상품/기간/산식/기준일/종료일·구조화된 사유로 남긴다. 예측 후 실적 불가면 예측값도 보존한다.
  잘못된 열/날짜/기간/중복 기준일·상품·산식 등 구조 오류는 BacktestInputError다.

## 사용 예

아래는 호출 형식이며 측정 결과가 아니다. product_daily는 호출자가 같은 소유자·창고의 유효 source에서 읽은 자료다.

```python
from datetime import date

from engine.backtest import SamplePeriod, generate_error_samples
from schemas.engine import Period

batch = generate_error_samples(
    product_daily,
    product_ids=[101],
    origins=[date(2026, 5, 1), date(2026, 5, 2)],
    horizon_days=10,
    period=Period(start=date(2026, 5, 1), end=date(2026, 7, 31)),
    period_name=SamplePeriod.TUNE,
    observed_through=date(2026, 7, 31),
)
```

## 검증과 남은 것

- 예측값 정답은 기존 DA03 손계산 표의 A/B를 그대로 재사용한다. 미래 실적에는 그 수량을 균등 배치해
  실적=예측 항등 관계와 배율 변환 관계를 검사한다. 새 수치 정답표를 AI가 사람 검산 완료로 표시하지 않는다.
- 기준일별 입력 차단·미래 실적 변화와 예측 불변·확정일 이후 상품 제외·기간 경계·불완전/무효/중복 실적·
  A/B 사용 불가·0 분모·overflow·SKU 격리·입력/순서 불변을 검사한다.
- 겹치는 창의 개수를 독립 표본 수/통계적 신뢰도로 해석하지 않는다. batch별 TUNE/TEST와 horizon/method 출처를 유지한다.
- 전체 run_backtest 응답/MAE·WAPE 집계·튜닝/정책 선택·테스트 구간 선택 편향 통제는 아직 구현하지 않는다.
  DA07의 SKU/POOLED 선택·최소 표본 수·비율의 도착 전/후 시나리오 재적용, DA13 등급/LOW 범위도 별도다.
- 공개 schema/HTTP/SQS·DB·기존 calculate/insight/engineVersion은 변경하지 않는다.
  이 내부 함수의 성공을 실제 위험·정확도·고객31상품·LLM/화면 전체 효과 또는 배포 완료로 확대하지 않는다.
