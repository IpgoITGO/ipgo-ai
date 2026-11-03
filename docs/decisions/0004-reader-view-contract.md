# 0004. FastAPI 조회 전용 view 계약

- 상태: 기존 구현 반영 (2026-11-03, AI #34 / Jira IPGO-103)
- 근거: Server V13·V14 migration, `UploadRecordService`·`UploadFileRepository`, AI `app/repository.py`.
  구현은 AI PR52·Server PR74에 병합돼 있다. 새 권한이나 schema를 추가하지 않는다.
- 관련: [0001 연동 계약](0001-engine-contract.md), [0003 월 경계·정제](0003-cleaning-connection.md).

## 소유권과 권한

Spring이 업무 데이터·상태·결과 저장을 소유하며, 별도 migration role이 view와 권한을 관리한다.
FastAPI는 `ipgo_ai_reader`로 아래 4개 view의 `SELECT`만 사용한다. 업무 table 직접 조회·DML·DDL·
migration은 허용하지 않는다. `DATABASE_URL`에는 이 조회 전용 role을 사용하며 값은 문서에 적지 않는다.

V13은 업무 table·sequence 권한과 schema CREATE를 reader에서 제거하고 일별 view SELECT를 부여한다.
이후 table의 reader 기본 권한도 제거한다. V14는 정제 view3개의 SELECT만 추가한다.
AI는 `read_only = True`인 transaction과 statement timeout을 사용하고 끝나면 rollback한다.
read-only 설정은 DB role 권한 검증을 대신하지 않는다.

view의 `security_barrier`는 회원별 RLS가 아니다. 요청 user/warehouse/source 소유권은 Spring과
AI repository가 확인한다. view에 요청 회원 필터가 자동으로 붙는다고 가정하지 않는다.

## 조회 목록과 전체 컬럼

| view | migration | 전체 컬럼 |
|---|---|---|
| `ai_product_daily_stocks_v1` | V13 | `product_id, warehouse_id, upload_file_id, stock_date, opening_qty, inbound_qty, outbound_qty, adjustment_qty, closing_qty, is_valid_observation` |
| `ai_cleaning_sources_v1` | V14 | `upload_file_id, user_id, warehouse_id, storage_key, original_name, period_start, period_end, revision_no, status` |
| `ai_cleaning_products_v1` | V14 | `user_id, sku_barcode` |
| `ai_cleaning_previous_units_v1` | V14 | `upload_file_id, user_id, warehouse_id, source_inventory_key, received_on, stock_date, closing_qty` |

### 계산용 일별 재고

`ai_product_daily_stocks_v1`은 `product_daily_stocks`와 `upload_files`를 연결하고 DONE source만 노출한다.
덮어쓴 이전 revision을 view 자체에서 제외하지는 않는다. Spring이 계산에 사용할 `uploadFileIds`를
고르며, AI `load_product_daily`는 같은 read-only transaction에서 sources view로 요청 ID 모두의
회원·창고·DONE 상태를 확인한 다음 일별 view를 `warehouse_id`와 `upload_file_id`로 제한한다.
누락·다른 회원/창고·중복 ID·미완료 source는 계산 입력 오류다. 단위는 엔진 schema를 따른다.

### 정제 source와 기존 상품

`ai_cleaning_sources_v1`은 `upload_files`와 `warehouses`를 연결한다. 현재 정제 source가 PROCESSING이고
이전 월 최신 revision은 FAILED일 수도 있으므로 view에 DONE 필터를 두지 않는다. AI는 현재 파일의
ID·회원·창고·저장 경로·원래 파일명·기간을 확인한다. 월 경계 이전 ID 선택은 [0003](0003-cleaning-connection.md)을 따른다.

`ai_cleaning_products_v1`은 `products`의 회원과 바코드 목록이다. `load_cleaning_context`가 요청
`user_id`로 제한해 기존 바코드를 읽고 `isNew` 판정에 사용한다. 이 view에는 활성 상품 필터가 없다.

### 이전 파일 재고관리단위별 월말 기말

`ai_cleaning_previous_units_v1`은 `daily_stock_records`·`inventory_units`·`upload_files`·`warehouses`를
연결하고 DONE source만 노출한다. AI가 검증한 `previousUploadFileId`, 회원, 창고로 제한해 읽는다.
`source_inventory_key`·`received_on`별 가장 늦은 `stock_date`를 선택하고 직전 월말인지 확인한다.
그 기말 재고를 이번 파일 첫날 기초 검산에 사용한다. 월말 누락·같은 날짜 중복·잘못된 수량·
요청과 다른 이전 source는 입력 오류다. 상세 규칙과 source 없는 경우는 0003을 따른다.

## 검증 근거와 한계

- 코드 대조: Server V13/V14의 view 컬럼·GRANT와 AI repository의 SELECT/필터가 위 표와 일치한다.
- 기존 2026-11-02 PostgreSQL16 V1~V14 검증에서 reader SELECT 성공·DML/DDL 거부를 확인했다.
  개발 migration/verify [run41453478403](https://github.com/IpgoITGO/ipgo-infra/actions/runs/41453478403)도 성공했다.
- 같은 날 실제 AWS 합성31상품·2개월 정제·계산에서 reader 조회와 쓰기금지, source 연결을 확인한
  배포 후 기록이 있다. 실제 고객 전체 흐름이나 최대 입력·장시간 부하의 검증은 아니다.
- 이 문서 변경에서는 DB/AWS를 재조회하거나 권한을 변경하지 않는다. mock cursor의 read-only 테스트와
  migration의 권한 검증은 별도 증거로 구분한다. 현재 배포 상태를 문서 작성만으로 재확인했다고 보지 않는다.
