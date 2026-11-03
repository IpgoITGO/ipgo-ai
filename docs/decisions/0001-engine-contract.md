# 0001. 계산 엔진 연동 계약

기준: API 명세서 4장. 스키마는 `schemas/engine.py`, `schemas/insight.py`.

## 결정 (2026-10-27)

| # | 항목 | 결정 | 이유 |
|---|---|---|---|
| 1 | 폴더 구조 | `app/` · `engine/` · `insight/` · `schemas/` · `tests/`. AI 인사이트 기존 코드는 `insight/`로 옮김 | 담당 폴더를 나눠 충돌을 줄임 |
| 2 | 스키마 원본 | `schemas/` 하나. AI 인사이트 코드도 여기서 import | 엔진 출력이 곧 인사이트 입력 |
| 3 | 정제 요청 파일 | `files` 배열의 원본은 정확히 1개. `uploadId`와 `files[0].uploadFileId`는 같고 `previousUploadFileId`는 nullable | 단일 파일 schema와 Spring payload는 PR52·Server PR74에 반영됨. 상세는 [0003](0003-cleaning-connection.md) |
| 4 | 정제 응답 일별 기록 | 행마다 `uploadFileId` 포함 | DB 행에 `upload_file_id`가 있음 |
| 5 | 입고 주기 추정 | 정제 응답 `estimatedReviewCycleDays` | S1 제안값은 수불부로만 계산 가능 |
| 6 | 계산 읽기 기준 | `uploadFileIds` + 확인용 `warehouseId` | 덮어쓴 파일 제외, `run_source_files`로 재현 |
| 7 | 소수 · 정수 | 엔진은 소수. 정수는 입고 단위로 올린 수량(권장 · 시나리오)과 금액뿐 | 기획서 "계산은 소수점까지, 화면에는 정수", ERD NUMERIC(18,3) |
| 8 | LOW 등급 예측 범위 | calculate 응답 `forecastRangeLow/High`, LOW일 때만 값 | `calculation_results`에 저장 |
| 9 | 명세에 없는 필드 | 422 `INVALID_INPUT`으로 거부 | 필드 이름 오타를 바로 발견 |
| 10 | 오류 형식 | `{"code", "message", "details"}`. 404 · 405도 같은 형식 | 명세 1장과 같아 Spring 변환이 쉬움 |
| 11 | mock 응답 | `ENGINE_MODE=mock`이면 명세 예시 반환 | 백엔드가 엔진 구현을 기다리지 않음 |
| 12 | 브랜치 · 의존성 · CI | 저장소 규칙(`feature/<issue>-*` → `dev`), `requirements/` 분리와 버전 고정, PR마다 ruff · pytest | 새 환경에서도 같은 결과 |
| 13 | 계산 불가 이유 | `NOT_CALCULABLE` · `EXCLUDED` · `REUPLOAD_REQUIRED`면 `reason` 필수. 없으면 스키마가 거부 | 0개로 바꾸지 않는 규칙을 코드로 보장 (`REUPLOAD_REQUIRED` 포함 여부는 백엔드 확인) |
| 14 | DB 소유권 | Spring이 업무 DB와 상태 변경을 소유. FastAPI reader는 아래 4개 view의 SELECT만 사용하고 업무 table 직접 조회·DML·DDL·migration은 금지 | 결과 정본과 변경 책임을 한 서비스에 둠. 목록·전체 컬럼·source 선택·검증은 [0004](0004-reader-view-contract.md) |
| 15 | 원본 저장 | AWS는 private S3. Spring은 읽기·쓰기, FastAPI는 읽기만 허용 | ECS task 사이 로컬 디스크를 공유하지 않음 |
| 16 | 긴 작업 전달 | 정제·계산·백테스트는 SQS job/result queue. Spring이 결과를 검증하고 저장 | HTTP timeout과 재시도 중복을 분리 |
| 17 | 짧은 작업 전달 | 시나리오·AI 인사이트만 private 내부 HTTP | 짧은 응답은 동기 계약이 단순함 |
| 18 | 업로드 사이 월 경계 | Spring이 같은 회원·창고의 직전 월 최신 revision을 먼저 고르고, 그 파일이 DONE일 때만 `previousUploadFileId`를 보냄. 없거나 최신 파일이 미완료/실패면 null. AI가 같은 선택 규칙과 전월 말 단위별 재고를 검증 | 덮어쓴 과거 DONE 파일을 재사용하지 않고 첫날 기초를 이전 월말 기말과 검산. PR52·Server PR74, [0003](0003-cleaning-connection.md) |

14~17은 2026-10-29에 `ipgo-server` #26·#27, `ipgo-ai` #15의 공통 AWS 목표 계약으로
확정했습니다. S3·RDS adapter와 SQS worker runtime은 구현되어 있습니다. 기본 HTTP 실행은
mock이며, real 정제·계산과 embedded worker는 기능별 검증 및 배포 승인 범위에서 사용합니다.
3·18은 2026-11-02에 병합된 AI PR52·Server PR74와 0003의 현재 계약을 2026-11-03에 반영했습니다.

### 월 경계 source와 검산 (#33)

- Spring `UploadRecordService`는 같은 회원의 창고에서 직전 월의 최신 revision을 선택한 뒤 DONE인지
  확인합니다. 먼저 DONE만 골라 과거 revision으로 되돌아가지 않습니다. 첫 업로드나 직전 월 자료가
  없을 때, 최신 revision이 PROCESSING/FAILED일 때는 `previousUploadFileId: null`을 보냅니다.
- FastAPI `CleanRequest.previous_upload_file_id`는 기본값이 None인 선택 필드입니다. DB가 설정된
  실행에서는 `load_cleaning_context`가 요청의 현재 원본 소유자·창고·PROCESSING 상태와 이전 ID를
  확인합니다. 서버와 다른 이전 ID, 다른 소유자/창고, 덮어쓴 과거 ID는 `INVALID_SOURCE`로 거부합니다.
- 이전 source가 있으면 재고관리단위(`source_inventory_key`, `received_on`)별 마지막 기록을 고릅니다.
  날짜가 직전 월말이고 이번 파일이 1일부터 시작할 때 이전 기말을 첫날 기초로 사용해 검산합니다.
  월말까지의 연속성이 없거나 이전 잔여 단위가 이번 파일에 없으면 입력 오류로 중단합니다.
- 이전 source가 없으면 이전 재고를 임의로 만들지 않고 기존 첫날 역산 규칙을 유지합니다.
  DB 없는 로컬 정제의 빈 context 허용은 개발 경로이며 실제 DB source 검증을 대신하지 않습니다.
- 구현 근거: AI `schemas/engine.py`, `app/repository.py`, `engine/clean.py`, Server
  `UploadRecordService`·`UploadFileRepository`. 경계 검증은 `tests/test_clean_connection.py`의
  월말 연결·최신 revision/소유권·월 경계 누락 검사와 Server의 업로드 revision 검사에 있습니다.
  이 문서 반영은 새 계산 정책·DB 변경이 아닙니다. source 상세는 [0003](0003-cleaning-connection.md)을 따릅니다.

## 조회 전용 DB 계약 (#34)

DB 조회 대상은 `ai_product_daily_stocks_v1`, `ai_cleaning_sources_v1`, `ai_cleaning_products_v1`,
`ai_cleaning_previous_units_v1`로 확정돼 있습니다(#34). Spring V13/V14와 AI repository가 같은
이름·컬럼을 사용합니다. 전체 컬럼과 소유권·source 선택 기준은 [0004](0004-reader-view-contract.md)를 따릅니다.
아래 목록은 이 조회 계약과 별도로 남은 항목입니다.

## 미정

- [ ] 입고 주기 추정 범위: 이번 파일만이 아니라 누적 기록으로 추정하는 방법
- [ ] 화면 정수 표시 규칙: 백엔드 제안은 "모두 올림". 재고(시작 · 잔여 · 날짜별)는 내림이 보수적이라는 의견 있음 (프론트와 확정)
- [ ] 자리 표시자 목록: `ending_p120` · `risk_post_p120` 추가 여부 (이석형) → [0002](0002-insight-contract.md) 3-4에서 함께 정함
- [ ] `/engine/insight` 입출력과 `ProductResult.insightInput`: [0002](0002-insight-contract.md)(#7)에서 바뀜. 합의 전까지 지금의 인사이트 mock을 기준으로 개발하지 않음
- [ ] LOW 등급 예측 범위 계산 방식 (DA-13 작업 때 장준영)
