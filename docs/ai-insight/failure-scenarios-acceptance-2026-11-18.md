# AI-01·AI-02 실패·대체 수용 (#44)

2026-11-17~18 KST. 사용자 승인으로 남은14행을 LGBM19 계약에 맞춰 검증했다.
공개 mode는 AI/FIXED/TABLE_ONLY, text는 단일 문자열/null이다. 알 수 없는 내부 실패는
공개 LLM_ERROR로 제한하고 원문 DB 사유는 보존한다. 정상·비활성·상태 고정 문장 사유는 null이다.

## 출처와 범위

- 실제 비공개 고정 weight + 합성 수불부7경계, 실제 GPT-4o mini30회는
  [모델 수용 기록](model-acceptance-2026-11-17.md)에 분리했다. 실제 고객 성과나 LLM 판사 평가가 아니다.
- r1: 독립 PostgreSQL18·Moto·실제 worker·FastAPI·Spring·최신 Client. 신규 합성 XLSX
  CLEAN/DONE→실제 양수 LGBM19 CALCULATE/DONE→Spring 모델 입력 준비를 확인했다.
- 그 결과의 시험 복사본에서 provider 응답 재생/장애·잘못된 상위 응답을 제어해 실패를 유도했다.
  실제 SDK/HTTP/DB/API/browser 경계를 통과한 수용이다. 외부 provider 장애가 실제 발생했다고
  주장하지 않는다. 실제 유료30회와 이 응답 재생의 호출·시간·비용을 합산하지 않는다.
- r1은70검사·브라우저13페이지, 후속 UI3는21검사·7페이지를 통과했다. UI3는 중지된 r1 DB의
  ro 물리 복사·파일 지문 일치를 확인하고 실제 설정 저장·새 모델 계산을 추가했다.
- 통합은 고정된 로컬 후보 image로 수행했다. 최종 필수 안내 키 guard는 기존 유료30응답의
  오프라인 재검사30/30와 전체 CI로 검증했다. 최종 ARM image의 소스 일치·개발 배포 확인은 별도다.

## 원래14행 판정

| # | 경계 | 실제 결과와 범위 |
|---|---|---|
| 1 | 정상 | 실제 worker 양수 모델 결과→Spring 준비→실제 LLM 응답의 제어 재생→AI/DB/API/화면 문장 일치. 별도 실제 유료30회 모두 통과 |
| 2 | LLM_TIMEOUT | 실제 SDK를 향한 제어 provider 지연→FastAPI FAILED→FIXED/LLM_TIMEOUT. 문장·비교표·화면·원장 시간 연결 |
| 3 | LLM_ERROR | 제어 provider500→실제 SDK 단일 실패→FIXED/LLM_ERROR·개별 안내. 자동 재호출0 |
| 4 | VALIDATION_FAILED | 금지 숫자/추천 문장의 제어 응답→실제 FastAPI 검사 실패→FIXED/VALIDATION_FAILED. 과거11/13 실패를 이 성공으로 수정하지 않음 |
| 5 | 값 대조 | 잘못된 PASSED/원시 숫자999를 상위 응답 경계에 주입→Spring VALUE_MISMATCH→모델 기본 문장·비교표 보존. 기대 숫자는 변경하지 않음 |
| 6 | FastAPI 무응답 | 제어 지연16초에 현행 Spring15초 제한→FIXED/LLM_TIMEOUT. SDK의10초 제한과 구분. 과거 계산 job timeout을 근거로 사용하지 않음 |
| 7 | real 키 없음 | 키 없는 독립 FastAPI 실제501→FIXED/LLM_NOT_CONFIGURED. 개발 Secret 제거0 |
| 8 | 비정상 insight 입력 | 독립 요청의 잘못된 필드→실제 Pydantic422→FIXED/INVALID_INPUT. XLSX 실패와 별개 |
| 9 | NOT_NEEDED | 실제 weight 합성 결과의0권장량·재고/출고 사실을 UI3 복사본에 연결. FIXED·사유null·LLM0, 모델 표시 이름·화면 문장 일치 |
| 10 | NOT_CALCULABLE | 실제 미지원 상품 결과의 사유·미산출 사실 연결. API 고정 안내·사유null, 실제 화면은 자료 부족 상세 경계이며 insight 요청0 |
| 11 | EXCLUDED/REUPLOAD_REQUIRED | 합성 상태 복사본의 실제 API/browser 경계. 인사이트 영역·요청0. 모델/정제가 이 상태를 산출하는 품질 시험으로 확대하지 않음 |
| 12 | 보류/확인 필요 | 현행 LGBM19는 해당 A/B 위험/등급 상태를 산출하지 않는다. 현행 ResultStatus·위험null과 대조해 비적용으로 확정. 가짜 위험·표본 부족·A/B 계산을 되살리지 않음 |
| 13 | 기본 문장도 없음 | 시험 payload의 fallback 부재와 잘못된 상위 응답→실제 TABLE_ONLY/text null. 비교표·안내·원장 보존 |
| 14 | 지난 계산 | 실제 K4에 없던 stale를 Server #256으로 제공, Client #433에서 안내. 실제 설정 PUT 후 계산 대기 중 true/기존 문장 보존, 실제 새 LGBM19 계산 후 새 결과 false·과거 결과 true. API/DB/browser 확인·새 LLM0 |

## 시간·비용·비간섭

각 요청의 실제 latencyMs, dispatchedAt/finishedAt, 공개 mode/failReason, 저장 문장과 비교표,
브라우저 원문·screenshot을 비공개 원장에 연결했다. provider 재생은 실제 외부 청구0이고,
상위 응답을 받지 못한 경우 토큰·비용 null을0으로 고치지 않았다. 이전 실제 유료 실패도 그대로 보존했다.
r1 보호 B DB와 두 큐 canary 지문은 동일했고, 관측기의 receive1/changeVisibility0 효과는 별도다.
UI3는 기존 문장·원래 실제 계산 행을 보존하고 새 LLM 원장 행이 늘지 않았음을 확인했다.

첫 UI2 실행기의 변수 이름 오류와 Docker 기본 주소 풀 소진은 실패 원본으로 남겼다.
기존 자원을 삭제하지 않고 충돌 없는 전용 internal subnet으로 보완했다. 시험 컨테이너는
생성 목록만 중지하고 DB 볼륨·망·원본을 보존했다. 실제 AWS·개발 서비스·고객 데이터 영향은 없다.

## 재현과 배포 경계

[로컬 구축 안내](../local-worker.md)의 독립 DB·두 큐·원본 ro와 고정 모델 경계를 사용한다.
실패 유도는 시험 환경에서만 수행하고 개발 key 제거·업무행 상태 강제 변경·공유 큐 purge를 하지 않는다.
Spring 통합 검사는 `InsightGenerationIntegrationTests`·`InsightIntegrationTests`,
Client는 `ScenarioInsight.test.tsx`·`resultApi.test.ts`, 모델 연결은
`test_model_insight_connection.py`에서 회귀를 확인한다.

개발 배포의 모델 설명은 OFF를 유지한다. 이 기록은 구현·격리 수용의 완료이며,
실제 고객 설명 활성화·운영 배포·장기 품질/효과 검증을 대신하지 않는다.
