# DA-01 로컬 embedded worker 실행·검증 (#57)

선행: [Infra #68 / PR #69](https://github.com/IpgoITGO/ipgo-infra/pull/69)의
[격리·접근 계약](https://github.com/IpgoITGO/ipgo-infra/blob/dev/docs/local-engine-isolation.md).
추적: [AI #57](https://github.com/IpgoITGO/ipgo-ai/issues/57) / Jira IPGO-108.
2026-11-17 독립 로컬 r4에서 실제 PostgreSQL18.7·Moto5.1.22·worker·Spring 저장의
정상/실패와 별도 LGBM19 계산을 수용했다. UI2는 정지 DB의 복사본에 대한 후검증이다.
아래 구축 안내와 과거 실측의 범위를 구분한다. 실제 AWS·유료 LLM 시험은 포함하지 않는다.

## 빈 로컬 환경부터 준비하는 순서

개발 DB·AWS 자격증명을 사용하지 않는 별도 시험 폴더를 만들고 `umask 077`을 적용한다.
Docker, Python3.14와 AI 개발 의존성, Server JDK21/Gradle·PostgreSQL client가 필요하다.
코드 SHA를 고정하고 승인된 private LGBM19 image/weight를 담당 경로에서 받는다.
공개 저장소에서 weight를 내려받거나 빈 bundle/mock으로 모델 수용을 대신하지 않는다.
최초 실측은 AI bf05c6e / Server ca08aa3 / Client 5baaf6e와
`lgbm19-60a4d15301cd5490`, 배송5일·주기14일·BUFFER100_FLOOR0를 사용했다.

1. 고유 이름을 정해 `docker network create --internal <시험망>`과
   `docker volume create <시험DB볼륨>`으로 이번 실행 전용 자원을 만든다.
   PostgreSQL18을 그 망의 `pg` alias로 실행하고 볼륨은 `/var/lib/postgresql`에 mount한다.
   DB명은 ipgo로 한다. 관리자 비밀번호는 실행 폴더의0600 private env file로 생성·주입하며
   `docker run --env-file <PG설정>`을 사용한다. 호스트 공용 PostgreSQL에 붙이지 않는다.
2. Server checkout의 `database/bootstrap/create_roles.sql`을 빈 DB에 관리자 stdin으로 실행한다.
   `ipgo_migrator`, `ipgo_server`, `ipgo_ai_reader`와 schema 소유권을 만든다.
   각 시험 역할의 비밀번호는 psql의 `\password`로 비표시 입력한다. 실제 개발 Secret을 복사하지 않는다.
   AI는 `ipgo_ai_reader`만 사용한다. 이 역할은 기본 read-only이며 V13/V14의 승인 view를 조회한다.
3. Server의 `src/main/resources/db/migration/V*__*.sql`을 **숫자 버전 순서**로 빈 DB에 적용한다.
   각 파일은 `psql -v ON_ERROR_STOP=1`에서 `SET ROLE ipgo_migrator;` 뒤에 stdin으로 실행한다.
   이 방식은 폐기 가능한 최초 시험 DB 전용이며 Flyway history를 만들지 않으므로 아래 Server의
   `SPRING_FLYWAY_ENABLED=false`를 고정한다. 실제 개발 DB의 Flyway 절차와 섞지 않는다.
   적용 파일 수·마지막 버전·실패0을 기록한다. V13/V14 view의 SELECT 성공과
   AI 역할의 업무 테이블 INSERT 거절을 확인한다. 새 migration이 있으면 전체 현재 파일을 포함한다.
4. 같은 망의 `moto` alias에 Moto5.1.22 server를 port5000으로 실행한다.
   metadata 조회를 차단하고 실제 AWS profile/key를 제거한 별도 프로세스에서
   `boto3.client('sqs', endpoint_url='http://moto:5000', region_name='ap-northeast-2')`로
   `create_queue(QueueName='jobs', Attributes={'MaximumMessageSize':'1048576'})`와
   results 큐를 만든다. SDK 인증값은 로컬 전용 가짜 값만 사용한다.
   생성된 두 QueueUrl을 Server와 AI에 동일하게 주입한다. URL을 임의로 조립하지 않는다.
5. 원본 트리는 이번 실행 전용으로 만들고 Server 실행 UID가 쓰도록 소유권을 지정한다.
   Server에는 `/data/raw` rw, AI에는 같은 트리를 `/data/raw:ro`로 mount한다.
   Server 저장 구조 `userId/UUID/original.xlsx`를 유지한다. 양쪽 `IPGO_UPLOAD_BUCKET`은 비운다.
6. Server는 전용 DB의 ipgo_server 계정, `IPGO_UPLOAD_STORAGE=local`,
   `IPGO_UPLOAD_DIR=/data/raw`, `IPGO_ENGINE_ENABLED=true`, `IPGO_ENGINE_TRANSPORT_ENABLED=true`,
   `IPGO_SQS_ENDPOINT=http://moto:5000`, 두 큐 URL을 사용한다.
   `IPGO_ENGINE_DEFAULT_METHOD=LGBM19`, 정확한 모델 버전을 지정한다.
   `IPGO_INSIGHT_ENABLED`, batch/single-result 설명, 결제·billing worker는 모두 false로 둔다.
   Flyway는 false, JPA ddl-auto는 validate다. JWT와 합성 사용자 로그인 비밀값은 새로 생성해0600 파일로
   주입한다. 개발 로그인·DB를 사용하지 않는다. 테스트용 합성 사용자를 UI 또는 Server 시험 fixture로 준비한다.
7. AI는 아래 설정 표에 더해 `ENGINE_MODE=real`, `IPGO_EMBEDDED_WORKER_ENABLED=true`,
   승인된 모델 directory, `DATABASE_URL`의 ipgo_ai_reader 계정, `STORAGE_BASE=/data/raw`,
   `AWS_ENDPOINT_URL_SQS=http://moto:5000`을 지정한다. API worker는1개다.
   OPENAI key를 주입하지 않는다. 모델 설명 스위치가 있는 버전은 OFF로 둔다.
8. Client를 사용할 때는 API proxy가 이 시험 Server만 가리키게 한다. `/upload`를 열기 전에
   Nginx 실행 UID가 index.html과 모든 asset을 읽을 수 있는지 확인한다.
   파일0644·폴더0755 등 읽기 권한을 맞추되 bytes/hash는 보존한다.403 페이지를 앱 성공으로 세지 않는다.

환경 주입 파일은 repo 밖에 보관하고 이름·설정 여부만 기록한다. Docker inspect 전체 env,
DSN·JWT·로그인 암호·원본 수불부를 로그나 PR에 싣지 않는다. 네트워크 연결은 기동 직전에 대조한다.

## 실제 수용과 중지

- 아래 health 확인 후 합성 정상 XLSX를 `/upload`에서 올리고 신규 uploadId/jobId가
  CLEAN→Spring DONE/attempt1로 이어지는지 DB와 결과 큐를 대조한다. 정상 수신만으로 완료하지 않는다.
- 지원 헤더가 없는 합성 XLSX를 별도 업로드하고 INVALID_SOURCE→Spring FAILED와
  화면의 실패 이유·다시 올리기를 확인한다. 정제를 수동 DONE으로 바꾸지 않는다.
- 후속 CALCULATE는 별도 job/run으로 기록하고 LGBM19 모델 버전·결과·설명 호출0을 확인한다.
  CLEAN 성공을 계산 성공으로 확대하지 않는다.
- 비간섭 검증이 필요하면 별도 B internal network/DB/Moto와 canary jobs/results를 준비해
  A에서 B DB·broker 및 외부443 접근 실패, B 업무행과 canary payload 지문의 전후 일치를 확인한다.
  관측기의 SQS receive/visibility 효과도 기록한다. 공유 개발 DB를 canary로 사용하지 않는다.
- 실행 종료 때 생성 목록의 컨테이너만 stop하고 in-flight 결과·ACK를 확인한다. 볼륨·망·로그와
  원본 실패는 보존한다. 데이터 삭제·volume rm·queue purge·docker prune은 하지 않는다.

기존 r4는 정상 CLEAN1·실패 CLEAN1·모델 CALCULATE1을 실제 처리했고 각각 attempt1이었다.
r4 UI403 실패 뒤 UI2는 정지 DB를 읽기 전용으로 물리 복사해 기존 결과5페이지17검사를 확인했다.
EXCLUDED/REUPLOAD_REQUIRED4건은 복사본 합성 경계이며 새 업로드·계산·LLM은0이었다.
이 문서 보완을 위해 같은 과거 수용을 다시 실행하거나 새 개발자 재실행을 완료 조건에 추가하지 않는다.

## 연결 대상과 담당

큐만 나눠도 공유 DB의 dispatcher가 다른 환경 작업을 선점·만료 처리할 수 있다.
개발 DB·큐·bucket·인증정보를 사용하지 않고 독립 시험 경계를 준비한다.
준비되지 않았으면 아래 기동·업로드 절차를 실행하지 않는다.

| 대상 | AI 설정과 확인 | 준비 책임 |
|---|---|---|
| DB | `DATABASE_URL`: 시험 DB의 read-only DSN. Server와 같은 시험 업무 DB·view를 읽는지 확인 | Infra: DB·접근 경계, Server: migration·view·role·시험 자료 |
| 메시지 | `AWS_ENDPOINT_URL_SQS`와 `IPGO_JOBS_QUEUE_URL`·`IPGO_RESULTS_QUEUE_URL`: 로컬 브로커와 서로 다른 시험 큐 쌍 | Infra: endpoint·격리·브로커 API/메시지 크기 계약, Server: 같은 큐 쌍 연결 |
| 파일 원본 | `STORAGE_BASE`: Server 시험 원본 트리의 AI 쪽 절대 경로. AI 읽기 전용, Server 쓰기 가능 | Infra: 경로·접근, Server: 원본 저장 |
| 결과 | AI는 결과 큐 발행까지만 담당. 업무 DB 저장·중복 처리·DONE/FAILED는 Server | Server와 공동 검증 |

Server의 `IPGO_SQS_ENDPOINT`는 AI의 `AWS_ENDPOINT_URL_SQS`와 같은 시험 브로커를 가리킨다.
컨테이너 안의 loopback은 호스트를 뜻하지 않는다. 컨테이너를 쓰면 Infra가 정한 서비스 주소를 사용하고
큐 URL도 두 프로세스에서 실제 접근 가능한지 확인한다. 브로커의 endpoint와 큐 URL 둘 다 대조한다.
SDK가 환경변수 endpoint를 지원하므로 새 endpoint 연결 코드는 추가하지 않는다.

파일 모드의 `storageKey`가 `userId/UUID`이면 AI는 `STORAGE_BASE/userId/UUID/original.xlsx`를 읽는다.
`IPGO_UPLOAD_BUCKET`이 설정되면 S3가 우선하므로 파일 모드에서는 비운다.
`db/` 원본을 선택하면 별도 `IPGO_ORIGINAL_DATABASE_URL`, `ipgo_ai_service_reader`와 원본 view가 필요하며
Server·Infra와 계약을 추가 확인한다. 파일 모드 안내를 그대로 사용하지 않는다.

## 설정 주입과 기동

Python 3.14의 개발 의존성을 설치한 `.venv`를 전제로 한다(README 실행 절차).
아래 명령은 환경 구축 명령이 아니며, 준비·승인된 시험 대상에 나중에 실행할 절차다.

1. `cp config/local-worker.env.example .env.local-worker` 후 `chmod 600 .env.local-worker`로 복사본을 보호한다.
2. Infra·Server와 시험 DB DSN·브로커 endpoint·큐 쌍·원본 트리를 대조해 빈 값을 채운다.
   `DATABASE_URL`이 비면 정제 문맥의 DB 소유권 조회를 건너뛰므로 통합 시험으로 인정하지 않는다.
3. Server의 datasource와 `IPGO_UPLOAD_STORAGE=local`·`IPGO_UPLOAD_DIR`, 시험 schema·view·role·합성 자료를 확인한다.
   개발 작업 테이블과 공유하지 않으며 `IPGO_ENGINE_ENABLED`·`IPGO_ENGINE_TRANSPORT_ENABLED`는 준비 후 활성화한다.
4. 실제 AWS key·개발 리소스 접근이 없고 유료 LLM도 비활성인 것을 확인한 뒤 복사본의
   `IPGO_EMBEDDED_WORKER_ENABLED`만 `true`로 바꾼다. 설정 파일·DSN·인증값을 출력하거나 commit하지 않는다.
5. 저장소 루트에서 다음 명령으로 API 한 프로세스와 embedded worker 하나를 시작한다.

```bash
env -i PATH="$PATH" PYTHON_DOTENV_DISABLED=1 \
  .venv/bin/python -m dotenv -f .env.local-worker run --override -- \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --workers 1
```

`env -i`로 기존 AWS profile·key·endpoint·DB·LLM 환경변수 상속을 제거하고 지정한 파일만 주입한다.
`PYTHON_DOTENV_DISABLED=1`은 앱의 자동 `.env` 로딩을 막는다. 별도 worker나 `--reload`를 함께 켜지 않는다.
이 주입 방식은 네트워크/DB 권한 격리 자체를 보장하지 않는다. 실제 접근 차단은 Infra 계약으로 확인한다.
worker는 `ENGINE_MODE=mock`이어도 실제 엔진을 호출하므로 mock을 안전 스위치로 사용하지 않는다.

## health·정상·실패·중지 확인

1. `curl --fail --silent --show-error http://127.0.0.1:8001/health`로 `status=ok`와 `mode=real`을 확인한다.
   worker thread가 죽으면 503이다. 수신 오류는 재시도하므로 health 200은 DB·브로커 연결이나 처리 성공 증거가 아니다.
2. Server/화면의 정상 업로드 경로로 **새 합성 XLSX**를 등록하고 새 uploadId·jobId를 기록한다.
   Server의 시험 `engine_jobs` 등록·전송, AI 원본 읽기·CLEAN `SUCCEEDED` 결과 발행,
   Server 결과 저장·업로드 `DONE`까지 같은 식별자로 대조한다. 결과 소비는 Server에 맡기고 수동 ACK하지 않는다.
3. 별도 새 업로드에 잘못된 합성 원본을 사용해 AI의 `FAILED`·`INVALID_SOURCE`와 Server의 사유 있는
   `FAILED` 전환을 확인한다. 잘못된 JSON envelope는 결과 발행 없이 재시도/DLQ 대상이므로 이 시험과 구분한다.
4. 정제가 자동 파생한 `CALCULATE` jobId와 계산 완료 여부를 별도로 기록한다.
   CLEAN 성공만으로 계산·LLM·전체 사용자 흐름 성공을 선언하지 않는다.
5. 폐기 가능한 독립 시험 환경 B의 작업·업로드·결과 큐가 A의 시험 전후 동일한지 Server·Infra와 확인한다.
   실제 개발 작업으로 부정 시험을 하지 않는다. 개발 worker 중지·purge·삭제·수동 상태 수정·재발행으로 맞추지 않는다.
6. 중지는 실행 터미널에서 Ctrl+C로 요청한다. 새 수신 중단과 프로세스 종료를 확인한다.
   in-flight 작업은 최대 30초 join 후에도 종료/재전달될 수 있어 결과·ACK를 별도로 확인한다.
   발행 실패는 jobs를 남기며, 발행 성공 후 삭제 실패는 중복 결과가 가능하므로 Server는 jobId로 중복 처리한다.

기록은 비밀값 없이 코드 SHA·실행 시각·시험 식별자·각 단계 결과·간섭 확인 범위·미검증 항목을 남긴다.
보존 기간·정리 담당은 Infra와 정하고 승인된 시험 자원에만 적용한다.
이번 PR에서 Server schema/view/role·dispatcher·업무 저장 코드는 변경하지 않는다.

## 환경 구축 없이 실행하는 검증

```bash
PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q \
  tests/test_local_worker.py tests/test_clean_connection.py tests/test_embedded_worker.py
```

신규 테스트는 임시 파일의 기존 합성 XLSX를 실제 storage·CLEAN·worker 경로로 처리하고
가짜 DB 문맥·큐를 사용해 정상/오류 결과 발행과 발행 실패 시 미삭제를 확인한다.
SDK endpoint 해석과 지정 파일 주입도 외부 연결 없이 검사한다. 계산 기대값·기존 fixture는 바꾸지 않는다.
실제 PostgreSQL·SQS 호환 브로커·Server 저장·두 환경 간 불변·컨테이너 mount는 검증하지 않는다.
오프라인 검사 통과만으로 연동 성공을 주장하지 않는다. #57 수용은 위 실제 r4/UI2 근거와
Infra의 확정된 격리·dispatcher·보존 계약 및 이 구축 안내를 함께 대조해 판정한다.
