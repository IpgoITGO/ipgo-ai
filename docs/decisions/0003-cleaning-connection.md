# 0003. 단일 원본 정제 연결

GitHub #47 / IPGO-111: 정제 요청은 원본 하나이며 `uploadId`와 `uploadFileId`가 같다.
`previousUploadFileId`는 nullable이다. Spring은 같은 회원·창고의 직전 월 최신 revision이
DONE일 때만 그 ID를 보낸다. 최신 revision이 PROCESSING 또는 FAILED면 과거 DONE으로
대체하지 않는다. AI는 status 필터 전에 최신 revision을 선택해 이를 검증한다.

Spring V14는 읽기 전용 view 세 개를 제공한다.

- `ai_cleaning_sources_v1`: `upload_file_id, user_id, warehouse_id, storage_key, original_name, period_start, period_end, revision_no, status`
- `ai_cleaning_products_v1`: `user_id, sku_barcode`
- `ai_cleaning_previous_units_v1`: `upload_file_id, user_id, warehouse_id, source_inventory_key, received_on, stock_date, closing_qty`

현재 source는 PROCESSING, 소유자·창고·ID·저장 경로·이름이 요청과 같아야 한다. 직전 기록은
단위별 마지막 날짜를 선택한다. 이 날짜가 전월 말이고 이번 자료가 1일부터 시작하는 경우에만
전월 closing을 첫날 opening으로 쓴다. 월말까지의 연속성을 확인할 수 없거나 전월 잔여 단위가
이번 파일에 없으면 입력 오류로 중단한다. 이전 자료가 없으면 기존 첫날 역산 규칙을 유지한다.
DB 설정이 없는 로컬 실행에서만 빈 context를 사용한다.

원본 행의 sheet·row와 단위 식별자를 보존하고 DA-01 검산 및 DA-02 관측 판정을 상품별로
반환한다. `CleaningInputError`는 `CalculationInputError`를 상속하며 HTTP는 422
`INVALID_SOURCE`로 반환한다. worker 담당 구현은 같은 예외를 먼저 잡아 `FAILED/INVALID_SOURCE`로
발행해야 한다. 현재 worker는 상위 예외를 잡아 `INVALID_INPUT`으로 발행한다. 요청 스키마 오류도
worker가 영구 재시도하지 않도록 CLEAN dispatch에서 정제 입력 오류로 변환한다.

실제 PostgreSQL view, S3, SQS 및 Spring 저장과 결과 크기 제한은 별도 통합 검증이 필요하다.
