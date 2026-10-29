# 0001. 계산 엔진 연동 계약

기준: API 명세서 4장. 스키마는 `schemas/engine.py`, `schemas/insight.py`.

## 결정 (2026-10-27)

| # | 항목 | 결정 | 이유 |
|---|---|---|---|
| 1 | 폴더 구조 | `app/` · `engine/` · `insight/` · `schemas/` · `tests/`. AI 인사이트 기존 코드는 `insight/`로 옮김 | 담당 폴더를 나눠 충돌을 줄임 |
| 2 | 스키마 원본 | `schemas/` 하나. AI 인사이트 코드도 여기서 import | 엔진 출력이 곧 인사이트 입력 |
| 3 | 정제 요청 파일 | 현재 mock HTTP schema는 `files` 배열을 유지하고, AWS worker 작업 1건은 파일 1개(`uploadFileId`)로 전환 | 서버의 업로드 1건이 파일 1개로 확정됨. schema 변경은 Spring 구현 PR과 함께 진행 |
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
| 14 | DB 소유권 | Spring이 업무 DB와 상태 변경을 소유. FastAPI는 승인된 계산용 view와 필요한 table의 `SELECT`만 허용 | 결과 정본과 변경 책임을 한 서비스에 둠 |
| 15 | 원본 저장 | AWS는 private S3. Spring은 읽기·쓰기, FastAPI는 읽기만 허용 | ECS task 사이 로컬 디스크를 공유하지 않음 |
| 16 | 긴 작업 전달 | 정제·계산·백테스트는 SQS job/result queue. Spring이 결과를 검증하고 저장 | HTTP timeout과 재시도 중복을 분리 |
| 17 | 짧은 작업 전달 | 시나리오·AI 인사이트만 private 내부 HTTP | 짧은 응답은 동기 계약이 단순함 |

14~17은 2026-10-29에 `ipgo-server` #26·#27, `ipgo-ai` #15의 공통 AWS 목표 계약으로
확정했습니다. 현재 mock router, local storage와 schema는 구현 PR 전까지 유지합니다.

## 미정

- [ ] 입고 주기 추정 범위: 이번 파일만이 아니라 누적 기록으로 추정하는 방법
- [ ] 화면 정수 표시 규칙: 백엔드 제안은 "모두 올림". 재고(시작 · 잔여 · 날짜별)는 내림이 보수적이라는 의견 있음 (프론트와 확정)
- [ ] 자리 표시자 목록: `ending_p120` · `risk_post_p120` 추가 여부 (이석형) → [0002](0002-insight-contract.md) 3-4에서 함께 정함
- [ ] `/engine/insight` 입출력과 `ProductResult.insightInput`: [0002](0002-insight-contract.md)(#7)에서 바뀜. 합의 전까지 지금의 인사이트 mock을 기준으로 개발하지 않음
- [ ] LOW 등급 예측 범위 계산 방식 (DA-13 작업 때 장준영)
