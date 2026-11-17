# CALCULATE 단계 시간과 작업 연결 (#121)

`IPGO_ACCEPTANCE_TIMING_ENABLED`는 기본 `false`다. `true`로 시작한 worker만
수신 시도 종료 시 `ai_calculation_timing` JSON을 한 건 기록한다. 내장 worker와
독립 worker는 전용 INFO logger를 사용하며 uvicorn의 root 로그 레벨에 의존하지 않는다.
같은 프로세스에서 handler를 중복 등록하지 않는다. 설정·배포·새 개발환경 시험은
승인 범위에 따라 진행하며 이 코드의 존재를 실제 관측 완료로 보고하지 않는다.

기존 개발 배포 workflow의 `acceptance_timing_enabled` boolean 입력도 기본 `false`다.
현재 one-box 내장 worker는 이 입력을 API task의 `IPGO_ACCEPTANCE_TIMING_ENABLED`로 받는다.
승인한 관측에서만 `true`로 dispatch하며 exact SHA·모델·worker/LLM·수량 등 다른 승인 입력은
그대로 유지한다. 관측 종료 후 같은 배포 경로에 `false`를 명시해 로그와 추가 SQS attribute
요청을 끌 수 있다. 이 토글의 존재는 배포/재시작·실제 메시지 생성 승인을 대신하지 않는다.
legacy 별도 worker task에는 이 workflow 입력을 자동 적용하지 않는다.

비활성 상태에서는 SQS 수신 옵션·기존 업무 로그를 유지하고 계측 시계·출력도 사용하지 않는다.
활성 상태에서는 기존 ReceiveMessage에 다음 system attribute만 추가 요청한다.
큐 속성이나 IAM·CloudWatch 리소스는 변경하지 않는다.

- SentTimestamp
- ApproximateFirstReceiveTimestamp
- ApproximateReceiveCount

## 고정 로그 필드

| 필드 | 의미 |
|---|---|
| event / schemaVersion | `ai_calculation_timing` / `1` |
| jobId / jobIdState | UUID 모양만 소문자로 기록. 나머지는 null / unsupported |
| runId | CALCULATE payload의 strict 양의 64비트 정수만 기록. 나머지는 null |
| outcome | success, result_failed, execution_failed, result_encoding_failed, publish_failed, delete_failed |
| resultStatus | 반환한 결과 봉투의 SUCCEEDED/FAILED. 결과를 얻지 못했으면 null |
| sentTimestampMs | SQS 발송 시각. 유효한 양의 밀리초 정수가 아니면 null |
| approxFirstReceiveTimestampMs | SQS가 제공한 근사 최초 수신 시각. 유효하지 않으면 null |
| receivedAtMs | 이 worker가 메시지를 처리하기 시작한 로컬 wall clock 시각 |
| approxReceiveCount | SQS 근사 수신 횟수. 누락·잘못된 형식은 null |
| clockState | ordered / inconsistent / missing_or_invalid |
| approxFirstQueueWaitMs | SQS 최초 수신 시각−발송 시각. 근사 최초 대기이며 재전달 대기가 아님 |
| messageAgeAtReceiveMs | 이번 로컬 관측 시각−발송 시각. 재전달·처리 시간을 포함할 수 있음 |
| currentReceiveQueueWaitMs | 최초 수신(count=1)에서만 최초 대기. 재전달에서는 null |
| redeliveryAfterFirstReceiveMs | count>1일 때 로컬 관측−최초 수신. 처리/visibility 지연을 포함하며 이번 대기라고 부르지 않음 |
| attemptElapsedMs | 시도 시작부터 삭제 요청 종료/실패까지 monotonic 경과. 로그 출력 시간은 포함하지 않음 |
| databaseRead | load_product_daily 전체 메서드의 elapsedMs / status. 미호출이면 null |
| execution | process_message의 elapsedMs / status. 입력 검사·engine slot 대기·DB/계산/결과 조립을 포함 |
| resultPublish | SQS 결과 발행 SDK 호출의 elapsedMs / status. Spring 저장 완료 시간은 아님 |
| messageDelete | 작업 메시지 삭제 SDK 호출의 elapsedMs / status |

각 단계의 status는 success/failed이며 본문·예외 내용을 보관하지 않는다. 정상적으로
FAILED 결과를 반환·발행·삭제한 경우 execution/resultPublish는 success이고 outcome은
result_failed다. 발행 실패는 삭제를 수행하지 않는다. 삭제 실패는 발행된 결과의 재전달을
유발할 수 있으므로 기존 Spring jobId 멱등 처리가 계속 필요하다.

`databaseRead`는 연결·소유권 검사·조회·rollback·DataFrame 조립을 포함한다.
SQL 실행만의 시간이라고 해석하지 않는다. 실행/시도/DB 시간은 중첩되므로 합산하지 않는다.
순수 추론·Spring의 결과 수신/DB 저장/commit 시간은 이 로그가 측정하지 않는다.
ContextVar는 이번 CALCULATE 처리 문맥에만 DB 시간을 연결하며 HTTP 요청이나 별도 thread에
자동 전달하지 않는다.

## 시계·보안·검증 한계

SQS attribute가 없거나 잘못됐거나 발송≤최초수신≤로컬관측 순서를 만족하지 않으면
대기 관련 값은 모두 null이다. 누락을 0으로 채우지 않는다. ordered는 순서 검사 결과이며
NTP 동기화 증명이 아니다. 감지할 수 없는 양의 clock skew가 messageAge/redelivery 경과에
포함될 수 있다. monotonic 경과가 역전돼도 null로 남긴다.

jobId가 임의 문자열이면 문자열·해시·본문을 로그에 남기지 않는다. runId 이외의 payload,
사용자/창고/상품 식별값·바코드·수량·원가·결과·큐 URL·ReceiptHandle·인증정보·원문 예외도
기록하지 않는다. 계측 로그 생성/출력 실패는 계산 결과·메시지 발행/삭제 동작을 바꾸지 않는다.

`tests/test_calculation_timing.py`는 가짜 큐/DB와 고정 시계로 의미·문맥 격리·실패 및 비노출을
검증한다. 실제 SQS·DB·서비스 부하 계측이나 #121 전체 수용 완료를 뜻하지 않는다.
