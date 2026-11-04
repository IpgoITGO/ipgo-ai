# 0002. AI 인사이트 연동 계약

- 상태: **결정 — 기존 합의 기록 반영 (2026-11-03, #10)**. 별도 미정은 5장에 한정한다.
- 근거: [AI24 결정](https://github.com/IpgoITGO/ipgo-ai/issues/24#issuecomment-6553257648), [Server25 답변](https://github.com/IpgoITGO/ipgo-server/issues/25#issuecomment-6517684510), PR45·49, [0001](0001-engine-contract.md).
- 원 합의/검토: 장준영·이석형, 서버 답변: 박혜윤. 현재 인계 반영: 이휘.
  정무영의 최종 확인은 AI24 종료 조건으로 유지하며 전원 최종 확인 완료로 기록하지 않는다.
- 관련: DA-11, AI-01~03·05~06, [prompt·기본문장](../ai-insight/prompt-and-fallback.md).

이 문서는 과거 값 dict·FastAPI 값 채우기/기본문장 제안을 위 합의대로 대체한다.
이전 제안과 검토 이력은 Git 이력과 AI24·Server25에 보존한다. 새 계산 정책·유료LLM 활성화 결정이 아니다.

## 1. 책임과 데이터 경로

| 기능 | 책임 |
|---|---|
| 계산 결과에서 인사이트 입력 생성 | 계산 엔진의 `to_insight_input()`; #36 구현, 아래 생성 조건 적용 |
| LLM 호출·초안 출력 검사 | FastAPI `insight/` |
| 숫자·문구 자리 표시자 값 삽입과 대조 | Spring, 저장된 계산 결과와 비교표 형식 사용 |
| 기본 문장 생성·실패 시 대체·화면 mode·DB 저장 | Spring |

엔진의 `ProductResult.insightInput`을 Spring이 저장하고 `{resultId, input}`으로
`POST /engine/insight`에 보낸다. FastAPI는 값이 없는 `{{이름}}` 초안을 돌려준다.
Spring이 숫자와 `formula_desc`·`risk_basis`·`label_*`·`action_*`·`dir_*` 문구까지 채우고 대조한다.
LLM에는 원본 수불부·원가·계정 정보·다른 SKU를 보내지 않는다.

#10은 전송 형식·mock을 반영했고 #36은 입력 변환을 구현한다. 현재 real 계산은 위험을 아직 산출하지 않아
`insightInput`이 계속 null이다. Server61의 입력 저장·재호출·문장 저장도 별도 구현이다.

### #36 입력 생성 조건

- `to_insight_input(result, sku_label, *, as_of_date=None)`은 `InsightInput | None`을 반환한다.
  별칭만 받으며 시나리오 조립 뒤 호출한다. 결과·요청·원래 수량/시나리오를 변경하지 않는다.
- `status=OK`, 산식·산식 대체 여부, 양수 권장량과 일치하는 비중복 P100, P80의 존재가 필요하다.
  P80은 중복 표시가 있으면 설명에서 제외한다. 같은 type 두 건이나 불명확한 P100을 임의 선택하지 않는다.
- 위험 상태 null이면 생성하지 않는다. OK는 실제 도착 전/설명 대상 시나리오별 위험·양수 표본 수·SKU/POOLED 기준이 필요하다.
  LOW_GRADE는 유한한 `0 ≤ forecastRangeLow ≤ forecastRangeHigh`가 필요하다.
  표본 부족·LOW_GRADE 입력에는 남아 있는 위험 수치도 넣지 않는다. 표본 부족 상태 자체를 여기서 판정하지 않는다.
- 산식 대체 시 사유, 시작 재고 추정 시 기준일/시작 재고가 필요하다. 필요한 사실이 빠지면 생성 보류(null)다.
- `build_catalog`는 prompt 문서5/5-1의 값이 있는 이름만 제공한다. 문구 키(†)는 FastAPI가 만들며
  실제 값은 계속 Spring이 채운다. 소진일 null은 '기간 안에 소진되지 않음'이라는 사실이므로 이름을 제공한다.
- 비교 방향은 prompt 문서6-4에 따라 표시 재고 FLOOR와 정수 %(`floor(100 × risk + 0.5)`)를 비교한다.
  원시 계산 결과는 반올림하거나 수정하지 않는다. NO_DATE는 이름과 boolean에만 반영한다.
- CUSTOM이 결과에 포함되면 다른 비권장 시나리오처럼 처리한다. CUSTOM을 결과에 합치는 방법이나
  저장·재생성 시점은 이 변환기가 정하지 않으며 아래 미정 범위를 유지한다.

## 2. 요청과 입력

외부 필드는 camelCase, Python은 snake_case다. 명세에 없는 필드는 거부한다.

| 필드 | 형식·의미 |
|---|---|
| `resultId` | 저장된 결과 ID; Spring이 요청에 붙임 |
| `input.skuLabel` | 실제 상품명 대신 쓰는 별칭 |
| `input.riskStatus` | `OK`·`INSUFFICIENT_SAMPLE`·`LOW_GRADE`. null은 유효한 입력이 아님 |
| `input.recommended` | `ScenarioType`, 보통 `P100` |
| `input.placeholders` | 자리 표시자 **이름 list**. 화면 값 dict를 받지 않음 |
| `input.directions` | `ending_p80_vs_p100`·`risk_post_p120_vs_p100` 등의 비교; 값은 `높음`·`낮음`·`같음` |
| `input.scenarios` | 중복을 제외한 설명 대상 `P80`·`P100`·`P120`·`CUSTOM` 목록 |
| `input.methodFallback` | 기본 산식 대신 다른 산식을 사용했는지 |
| `input.startStockEstimated` | 시작 재고가 추정값인지 |
| `input.hasNoDateReceipts` | 도착 예정일 없는 미반영 발주가 있는지 |

`status`와 화면 형식 값 dict는 InsightInput에 넣지 않는다. Spring은 `status=OK` 결과만 호출한다.
계산 불가·입고 불필요·검산 제외·재업로드 필요는 FastAPI를 호출하지 않고 Spring이 처리한다.
비활성·정책 결정일 이전 등 `riskStatus=null`인 미산출을 임의 `INSUFFICIENT_SAMPLE`로 바꾸지 않는다. 실제 상태를 산출하기
전까지 입력을 만들지 않으며, 생성 보류/화면 처리는 실제 연결 작업에서 명시한다.

기존 요청 호환을 위해 새 메타데이터는 `scenarios=[]`, bool3개=false를 기본으로 수용한다.
새 변환기는 실제 계산 결과의 값을 명시적으로 채워야 한다. 이 기본값을 real 계산의 상태 판정 근거로 쓰지 않는다.
비어 있지 않은 scenarios를 보내면 qty 자리 표시자의 시나리오들과 일치하고 중복이 없어야 한다.
이번 #10은 prompt/검사 규칙을 바꾸지 않으며 새 메타데이터에 따른 문장 보강은 AI 담당 작업과 조율한다.

```json
{
  "resultId": 9001,
  "input": {
    "skuLabel": "상품 A", "riskStatus": "OK", "recommended": "P100",
    "scenarios": ["P100"], "methodFallback": false,
    "startStockEstimated": false, "hasNoDateReceipts": false,
    "placeholders": ["qty_p100", "ending_p100", "risk_post_p100", "risk_pre"],
    "directions": {}
  }
}
```

위는 형식 예시이며 완전한 생성 입력 예시는 `tests/payloads.py`와 mock calculate다.
운영 입력에 필요한 최소 이름은 prompt 문서5-1을 따른다. AI53의 예시·키 보강 뒤에는 목록을 함께 대조한다.
자리 표시자는 snake_case와 시나리오 접미사를 쓴다: `qty_p100`, `ending_p120`, `risk_post_p120` 등.
감싸는 문법은 `{{이름}}`이다. 실제 값은 list에 넣지 않고 Spring이 원래 계산 결과에서 꺼낸다.

## 3. 계산 계약의 메타데이터

| 필드 | 계약 |
|---|---|
| `CalculateRequest.noDateReceipts` | `[{productId, quantity}]`, 상품 ID/정수 수량은 양수. 기존 요청 기본값은 빈 목록 |
| `ProductResult.noDateReceiptQty` | 해당 결과 상품의 noDateReceipts 수량 합계, 기본0. **권장량·재고 시뮬레이션에 사용하지 않음** |
| `ProductResult.methodFallback` | 산식이 있는 결과에서 요청 기본 방식과 실제 선택 방식이 다른지; 산식 미산출은 null |
| `ProductResult.riskBasis` | `SKU`·`POOLED` 또는 미산출 null. DA-07 구현 전 SKU로 추측하지 않음 |
| `ProductResult.insightInput` | `InsightInput` 또는 null. 위험 상태가 일치하는 OK 결과에만 제공 가능 |

noDateReceipts가 같은 상품에 여러 건이면 메타데이터 수량을 합산하고 다른 상품 건은 섞지 않는다.
이 메타데이터만으로 새 상품을 계산 대상으로 추가하지 않는다. 기존 `plannedReceipts`는 도착일 있는 발주만 담는다.
전송 형식·메타데이터 추가로 engineVersion은0.4.0이며 기존 DA03/04/06 계산식과 손계산 fixture는 유지한다.

pooled 위험은 기존 DA-07 요구사항을 유지한다. SKU 표본 부족 시 전체 상품 오차 표본을 사용하고,
전체로도 부족하면 INSUFFICIENT_SAMPLE로 판정한다. 실제 구현/검증은 #36 후속이다.
LOW_GRADE의 예상 출고량 범위와 null 위험도 역시 기존 의미를 유지하며 임의 수치를 만들지 않는다.

## 4. FastAPI 응답과 Spring 처리

| 필드 | 의미 |
|---|---|
| `validation` | `PASSED`·`FAILED` |
| `draft` | 통과한 자리 표시자 초안 str; 실패하면 null |
| `failReason` | `LLM_ERROR`·`LLM_TIMEOUT`·`VALIDATION_FAILED`, 통과하면 null |
| `promptVersion` | 실제 생성 서비스가 사용한 PROMPT_VERSION; 모의 응답은 `mock` |
| `latencyMs`·`costUsd` | 기존 시간·비용 필드 유지. 호출 오류 등 비용을 모르면 기존 계약대로0 |

FastAPI는 `mode`, `templateVersion`, `fallbackDraft`를 만들지 않는다. 상세 검사 사유는 현재 로그에
남기며 이번 #10에서 `failDetail`이나 모델/토큰 필드를 임의 추가하지 않는다. 추가 전송 범위는 별도 합의한다.
LLM의 JSON 문장 배열은 FastAPI가 검사해 `draft` 하나의 글로 돌려주는 현재 동작을 유지한다.
Spring 공개 API의 문장 배열과 `AI/FIXED/TABLE_ONLY` 화면 mode는 Server25/61·AI44에서 맞춘다.

LLM 시간 초과·호출 오류·검사 탈락은 HTTP200 + FAILED/null draft다. 키 없음501·잘못된 입력422와
무응답/시간 제한 초과도 Spring이 구분해 기본 문장으로 대체한다. 값 대조 실패는 Spring에서만 발생한다.
기본문장 원본은 prompt 문서6장이며, 숫자 형식은 Spring 비교표와 동일하게 맞춘다.

## 5. 별도로 남은 것

- AI24의 정무영 최종 확인과 Server API 명세 반영/저장 연결. 기존 합의 기록 자체를 미정으로 되돌리지 않는다.
- AI44의 최종 화면 상태 이름·추가 실패 사유·문장 형태·연동 시험 날짜.
- CUSTOM 입력 갱신과 문장 재생성 시점/범위.
- AI53의 no_date_receipts 설명·정상 예시 ending_p80·필수 키표/버전 보강.
- DA-07의 실제 원장 업로드/업무 검증, 전체 백테스트/정확도·실제 LLM/화면 전체 검증.
  DA07 내부 계산과 HTTP/SQS 공급은 [0007](0007-da07-runtime-sampling.md)을 따르며 #36 변환 구현과 구분한다.

이번 계약/변환 검증을 DA-07 산출·Server 저장·LLM 활성화나 전체 업무 효과 완료로 보지 않는다.
