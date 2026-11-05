# 0005. DB 원본 조회 계약 (Server #76 / BE-03)

상태: 2026-11-05 승인 정책을 최신 Server dev e92487f / AI dev 297cef4에 로컬 통합·보완. 실제 DB V15 migration·배포·기존 파일 이전은 미실행이다.
기존4개 view 계약은 [0004](0004-reader-view-contract.md)에 유지한다.

서버는 opt-in DB 모드에서 원본을 `upload_originals.content` bytea에 업로드·job과 같은 transaction으로 저장한다.
기존 원본은 `EXTERNAL`, 새 DB 원본은 `DATABASE` marker와 `db/<userId>/<uuid>` storageKey로 구별한다.
기존 envelope contractVersion/SourceFile 필드는 변경하지 않는다. 새 key를 처리하지 못하는 AI에 DB 작업을 보내지 않는다.

V15가 추가하는 `ai_upload_originals_v1` 컬럼:

`upload_file_id, user_id, warehouse_id, storage_key, original_name, status, sha256, content_type, size_bytes, content`

`ipgo_ai_service_reader` 전용 role만 이 view를 포함한 승인5개 view의 SELECT를 받으며 원본/업무 테이블 직접 조회·DML·DDL은 받지 않는다.
이 view는 회원별 RLS를 대신하지 않는다. AI가 현재 source·이전 월·소유권을 기존 방식으로 확인하고,
DB 원본 SELECT에서도 uploadId·userId·warehouseId·storageKey·originalName·PROCESSING을 함께 제한한다.
대입값은 SQL parameter로 전달한다. 10 MiB 초과 선언은 CASE로 binary를 읽지 않으며 실제 길이·SHA-256·MIME도 검사한다.
read-only transaction·5초 연결/10초 SQL timeout·rollback을 유지한다.

`db/` source는 `IPGO_ORIGINAL_DATABASE_URL`만 사용하고 실제 current_user가 `ipgo_ai_service_reader`인지 확인한다.
기존 DATABASE_URL로 대체하거나 공유/관리자 계정으로 읽지 않는다. 기존 reader/사람용 계정에는 원본 권한을 부여하지 않는다. 원본이 없거나 소유권·길이·해시가 다르면 `INVALID_SOURCE`로 실패하고
local/S3로 대체하지 않는다. DB 미설정/장애는 기존 오류·재시도 경로다. 외부 key는 기존 local/S3를 사용한다.
원본을 엔진 밖 LLM에 보내거나 쓰기 권한을 추가하지 않는다. engine·prompt·계산/기존 사람 기대값은 변경하지 않는다.

적용 순서는 승인·백업 → V15 → role/view 확인 → 호환 AI → 서버 코드 → 별도 승인한 DB 저장 모드/총량 설정이다.
승인된 DB 원본 정책은 총256MiB·최소30일보존·신규 업로드만 DB 저장이다. 기존 S3/local 원본은 유지한다.
기간과 총량은 V15 제약/trigger·서버 검사로 검증하며 원본과 구 revision을 자동 삭제하지 않는다.
이 한도는 실제 WAL/백업/메모리 수용 성능의 실측값이 아니다. 운영 Secret 주입·권한/자원/복구 확인과 실제 적용 승인은 별도다.
기존 local/S3의 이전은 별도 작업이다. 저장 모드를 되돌려도 이미 생성한 DB 원본 reader는 유지해야 한다.
table DROP이나 구 AI로 무조건 돌아가는 방법은 복구 절차가 아니다.

서버 설계 문서 `docs/decisions/0003-database-original.md`와 같은 계약이다.
DB 원본 정제 완료·후속 계산·LLM·브라우저·AWS 실측은 각각 구분해 검증한다.

과거 2026-11-03 로컬 검증: 관련pytest28개·Ruff와 격리PG16/실제Spring/Moto/AI worker의 합성3상품×31일
정제→DB저장, bytes/크기/SHA 일치·다른 회원/창고 거절·실패 경로가 통과했다. 로컬/S3는 설정하지 않았다.
실제 AWS·브라우저·업무 원본·큰 메시지·최대 부하·후속 계산/LLM을 검증한 것은 아니다.


## 로컬 검증 — 2026-11-05

최신 dev 통합 후 전체 pytest579개·Ruff 통과. 원본 전용 연결 사용과 공유/서버/관리자 role 거부, DB source의 외부 fallback 금지를 검사했다.
실제 로컬 Spring/AI worker/PG16/Moto 연결22개 검사를 통과했다. 신규DB 원본 정제→저장 DONE·해시/크기 일치와
기존 S3 원본 bytes/metadata/정제 경로 보존, owner/warehouse 거부·원자적 rollback·동시quota·보존 정책을 확인했다.
서버 JDK21 검사295중292통과/3조건부생략·bootJar·로컬V1~V15 및 DB작업이미지 검사는 별도 서버 결과다.
이것은 합성 자료·폐기용 로컬 인프라 검사이며 실제 AWS/S3 업무 자료·30일 장기 관측·WAL/복원·유료LLM·배포 검증이 아니다.
실제 V15·계정/Secret/IAM·배포·원본 이전/삭제는 이번에 실행하지 않았다.
