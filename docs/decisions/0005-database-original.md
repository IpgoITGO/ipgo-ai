# 0005. DB 원본 조회 계약 (Server #76 / BE-03)

상태: 로컬 후속 구현. 실제 DB V15 migration·배포·기존 파일 이전은 미실행이다.
기존4개 view 계약은 [0004](0004-reader-view-contract.md)에 유지한다.

서버는 opt-in DB 모드에서 원본을 `upload_originals.content` bytea에 업로드·job과 같은 transaction으로 저장한다.
기존 원본은 `EXTERNAL`, 새 DB 원본은 `DATABASE` marker와 `db/<userId>/<uuid>` storageKey로 구별한다.
기존 envelope contractVersion/SourceFile 필드는 변경하지 않는다. 새 key를 처리하지 못하는 AI에 DB 작업을 보내지 않는다.

V15가 추가하는 `ai_upload_originals_v1` 컬럼:

`upload_file_id, user_id, warehouse_id, storage_key, original_name, status, sha256, content_type, size_bytes, content`

AI role은 이 view의 SELECT만 추가로 받으며 원본/업무 테이블 직접 조회·DML·DDL은 받지 않는다.
이 view는 회원별 RLS를 대신하지 않는다. AI가 현재 source·이전 월·소유권을 기존 방식으로 확인하고,
DB 원본 SELECT에서도 uploadId·userId·warehouseId·storageKey·originalName·PROCESSING을 함께 제한한다.
대입값은 SQL parameter로 전달한다. 10 MiB 초과 선언은 CASE로 binary를 읽지 않으며 실제 길이·SHA-256·MIME도 검사한다.
read-only transaction·5초 연결/10초 SQL timeout·rollback을 유지한다.

`db/` source는 `DATABASE_URL`만 사용한다. 원본이 없거나 소유권·길이·해시가 다르면 `INVALID_SOURCE`로 실패하고
local/S3로 대체하지 않는다. DB 미설정/장애는 기존 오류·재시도 경로다. 외부 key는 기존 local/S3를 사용한다.
원본을 엔진 밖 LLM에 보내거나 쓰기 권한을 추가하지 않는다. engine·prompt·계산/기존 사람 기대값은 변경하지 않는다.

적용 순서는 승인·백업 → V15 → role/view 확인 → 호환 AI → 서버 코드 → 별도 승인한 DB 저장 모드/총량 설정이다.
실제 DB 보관량·기간·WAL/백업/메모리는 운영 적용 전에 결정한다. 현재 원본과 구 revision은 자동 삭제하지 않는다.
기존 local/S3의 이전은 별도 작업이다. 저장 모드를 되돌려도 이미 생성한 DB 원본 reader는 유지해야 한다.
table DROP이나 구 AI로 무조건 돌아가는 방법은 복구 절차가 아니다.

서버 설계 문서 `docs/decisions/0003-database-original.md`와 같은 계약이다.
DB 원본 정제 완료·후속 계산·LLM·브라우저·AWS 실측은 각각 구분해 검증한다.

2026-11-03 로컬 검증: 관련pytest28개·Ruff와 격리PG16/실제Spring/Moto/AI worker의 합성3상품×31일
정제→DB저장, bytes/크기/SHA 일치·다른 회원/창고 거절·실패 경로가 통과했다. 로컬/S3는 설정하지 않았다.
실제 AWS·브라우저·업무 원본·큰 메시지·최대 부하·후속 계산/LLM을 검증한 것은 아니다.
