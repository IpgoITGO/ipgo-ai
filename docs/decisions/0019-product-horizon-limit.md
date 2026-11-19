# 0019. 상품별 계산 기간 366일 제한은 그 상품만 계산 불가 (#141)

## 배경

11/18 dev QA 계정(창고 32)에 리드타임 23일 · 입고 주기 555일(합계 578일) 상품이 하나 있었다.
`validate_calculation_capacity`가 366일을 넘는 상품이 하나라도 있으면 요청 전체를
`CalculationInputError`로 막아 169개 상품 계산이 모두 실패했다(run 502 ~ 514).

## 결정 (2026-11-18)

| 항목 | 결정 |
|---|---|
| 366일 초과 상품 | 그 상품만 `NOT_CALCULABLE`, `reason` = "리드타임과 입고 주기를 합쳐 366일 이하로 넣어 주세요". 나머지 상품은 계산 |
| 경계 | 리드타임 + 입고 주기 366일은 계산, 367일부터 계산 불가 |
| 결과 모양 | 엔진의 다른 계산 불가 결과(`_not_calculable`)와 같다. 결과 순서는 원래 상품 순서, `noDateReceiptQty` · `insightInput` · `accuracyEvaluation`도 같은 규칙으로 채운다 |
| 요청 전체 제한 | `today - asOfDate` 730일, 시나리오 예상 결과 60,000개는 지금처럼 요청 전체 실패 |
| 시나리오 점수 합계 | 366일 초과 상품은 시나리오와 일별 재고를 만들지 않으므로 합계에서 뺀다 |
| 위험 표본 | 366일 초과 상품의 입고 설정은 위험 표본 길이(H, D) 계산에서도 뺀다. 표본 pool(상품 범위)은 바꾸지 않는다 |
| 직접 입력 시나리오 | 고른 상품이 366일 초과면 기존 계산 불가 처리처럼 같은 이유로 `INVALID_INPUT`(422). 같은 입력에 다른 366일 초과 상품 설정이 있어도 고른 상품이 정상이면 계산 |
| 적용 경로 | HTTP `/engine/calculate` · SQS CALCULATE · HTTP `/engine/scenarios`가 모두 `app/calculation.py`의 같은 규칙을 쓴다 |

## 위치

366일은 계산식이 아니라 서비스 보호 한도라서 `app/execution.py`에 두고, 상품을 빼고 결과를 합치는 일은
`app/calculation.py`가 한다. `engine/`의 계산식과 함수는 바꾸지 않았다.

## 함께 바뀌는 곳

- Server: IpgoITGO/ipgo-server#257에서 입고 조건 저장 때 366일 초과를 막는다. 이미 저장된 상품을 위해
  AI도 상품 단위로 처리한다.
- Server: IpgoITGO/ipgo-server#258에서 AI 실패 이유를 저장한다.
- 요청 · 응답 schema는 바뀌지 않는다.
