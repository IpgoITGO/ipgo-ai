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
반환한다. checked는 전체 상품 수이고 target은 검산에서 제외된 고유 상품 수를 뺀 값이다.
`CleaningInputError`와 잘못된 CLEAN payload는 HTTP 422 또는 worker `FAILED/INVALID_SOURCE`다.
동일 바코드의 원가가 서로 다르거나 입고일 전 재고 기록이 있으면 원본 오류다.

계산에 필요한 모든 업로드는 같은 read-only DB transaction의 `ai_cleaning_sources_v1`에서
회원·창고·DONE을 확인한 다음 일별 view를 조회한다. DB가 없으면 계산을 진행하지 않는다.
XLSX 압축 해제와 정제 격자, 계산 기간 및 프로세스 동시 실행에는 README의 용량 제한을 적용한다.
이 한도는 계산 정책이 아니다. worker는 실패한 수신을 재시도하고 발행 후 삭제하여 at-least-once를
유지한다. 프로세스별 단일 실행 잠금은 별도 프로세스와 공유되지 않는다.

실제 PostgreSQL view, S3, SQS 및 Spring 저장과 결과 크기 제한은 별도 통합 검증이 필요하다.
