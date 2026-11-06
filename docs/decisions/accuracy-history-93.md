# #93 현재 계산의 정확도 이력 계약

2026-11-06 사용자가 권장 기준과 로컬 구현·검증을 승인했다. 배포는 제외한다.

## 의미와 범위

- 별도 BACKTEST 작업을 만들지 않고 CALCULATE의 기존 accuracy_for 결과를 재사용한다.
- 2026년 5~7월 TUNE·고정 주간 기준일의 과거 자료 재생이다. 당시 발행 예측의 실측 성과, 독립 TEST 성과 또는 고객 업무 효과를 뜻하지 않는다.
- 각 originDate 다음 날부터 H=leadTimeDays+reviewCycleDays일의 예측/관측 출고 합계다. 현재 상품의 실제 사용 산식·요청 배수를 적용한다. 미래 실제 입고를 예측 입력으로 제공하지 않는다.
- originDate+H는 정책 종료일과 asOfDate 이하이어야 한다. 겹친 창을 독립 표본으로 합산하지 않는다. covered는 재생 기간 내 모든 날의 기말 재고가 양수인 경우다.
- 모든 유효 이력을 보존하고 날짜순 반환한다. 현재 정책 기준일은 최대13개이고 H/관측 마감에 따라 더 적다. 위험 비율의 최소20개와 이력 표시 여부는 별개다.

## 내부 전달과 저장

ProductResult.accuracyEvaluation은 선택적이다. schemaVersion=1, status=AVAILABLE/EMPTY/UNAVAILABLE, reason, periodStart/periodEnd, observedThrough, horizonDays, method, multiplier, policyVersion/policyHash, dataHash, 원본 소수 history를 전달한다.

- AVAILABLE은 유효 이력이 있다. EMPTY/NO_VALID_SAMPLES는 평가했지만 유효 사례가 없다. UNAVAILABLE/EVALUATION_UNAVAILABLE은 정책 비활성·확정 전·계산 불가 등의 이유로 평가하지 못했다. 세부 운영 원인을 새 기능으로 추적하지 않는다.
- 현재 서버는 da07-2026-tune-weekly-v1 정책과 canonical policy hash 8594706942cd7bca0c7e73cabf4c242d02d9d731eaa1d23461038e6118f6ee6d를 수용한다. 새 정책은 서버 계약을 먼저 확장해야 한다.
- policyHash는 정책 파일 JSON 객체를 키 정렬·공백 없는 JSON으로 정규화한 SHA256이다. 배포 파일 원본의 바이트 hash와는 구분한다.
- dataHash는 선택 uploadFileIds·asOfDate 이하의 일별 입력을 대상으로 한다. 허용 열을 이름순 정렬하고 행을 product_id/stock_date/upload_file_id순 정렬한다. 열 배열 JSON과 각 행 배열 JSON 뒤에 각각 LF를 붙여 SHA256을 누적한다. ID는 정수 문자열, 날짜는 ISO, 관측 여부는 boolean, 수량은 유한한 float.hex 문자열이며 음의0은0으로 통일한다. JSON은 공백 없는 UTF-8이다. 설정·파일 ID·engineVersion은 기존 snapshot과 결합해 재현한다.
- 서버는 필드·정책·날짜/중복·H/산식/배수·숫자·이력 건수/성공수·원본 WAPE를 검증한 후 기존 거래 안에서 selection_snapshot.resultFacts[productId].accuracyEvaluation에 저장한다. 기존 requiredQtyBeforeRounding과 함께 보존한다.
- 구버전 필드 누락은 허용하고, 명시된 필드가 손상됐으면 부분 저장 없이 거부한다. 권한/대상 검사는 기존 job/run/source 경계를 따른다. 저장된 dataHash 자체를 원본 DB와 독립 재계산해 인증하는 기능은 아니다.

## K1과 화면

K1 accuracy.history의 기존 originDate/forecastQty/actualQty/covered를 유지한다. 예측/관측 출고는 원본 소수를 각각 올림한다. 저장 합계·WAPE를 표시 정수로 재계산하지 않는다.

accuracy.evaluation에는 status/reason/source=HISTORICAL_REPLAY/period=TUNE/기간/관측 마감/H만 제공한다. 내부 hash·원본 ID를 노출하지 않는다. 이전 결과는 history=[]/evaluation=null이다. accuracy 자체가 null인 기존 조건은 그대로다.

화면은 연도를 포함한 기준일, H일 합계, 과거 재생/TUNE/겹친 창·표시 올림의 의미를 안내한다. 표본 없음·평가 미실행·기록 없음을 구분한다. GET에서 계산하거나 과거 결과를 소급 갱신하지 않는다.

## 검증과 복구

- AI: 손계산이 가능한 하루1.25개×10일=12.5개 합성 자료, 기존 지표 불변·미래 자료 차단·순서와 hash 재현·빈/비활성·HTTP/worker 일치.
- 서버: 해당 AI 응답 fixture의 저장/K1 및 잘못된 이력 원자적 거부, 원본12.5개→화면13개, 중복·이전 결과 보존. H2와 격리 PostgreSQL의 실행 결과는 로컬 통합 검증 보고서에서 구분한다.
- Client: 서버가 출력한 합성 K1 fixture를 변환해 출처·기간·그래프·빈 상태를 검사한다. 단계별 fixture 전달이며 실제 AWS나 브라우저→실행중 서버의 연속 E2E로 보고하지 않는다.
- 새 테이블/migration/정책 재선정/LLM/실업무 재계산은 없다. 데이터 삭제 없는 이전 코드 SHA 복귀가 복구 방법이며, 배포는 별도 승인이 필요하다.

## PR 전 검토 보완

- 테스트의 고정 임시 경로에 결과를 쓰는 부작용을 없애 CI의 사전 디렉터리 생성에 의존하지 않는다.
- 비어 있는 DataFrame도 기존 계산과 같은 정규화를 사용해 계산 불가 응답을 유지한다.
- 서버는 상세 조회에서 같은 run의 facts를 한 번만 읽어 이력과 올림 전 필요량에 공유한다.
- 화면은 이력1건도 점으로 표시하고, 실제 출고0으로 WAPE가 미산출일 때 커버 평가와 구분해 안내한다.
