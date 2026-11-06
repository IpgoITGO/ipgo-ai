# 인사이트 모델·토큰·비용 전달 — Server #33

목표는 기존 LLM 호출기에서 얻은 관측을 Spring 저장까지 연결하는 것이다. 모델/프롬프트/호출 횟수/재시도 정책은 바꾸지 않는다.

`InsightResponse`는 기존 validation/draft/failReason/latencyMs/promptVersion과 nullable modelName/inputTokens/outputTokens/costUsd를 전달한다.
modelName은 제공사가 응답한 스냅샷 이름이다. 제공사 이름·usage가 없으면 요청 설정이나 추정 토큰으로 채우지 않는다.
관측된 0은 0으로 남는다. costUsd는 기존 단가표 추정치로, 청구액이 아니며 usage/단가 미확인·호출 오류는 null이다.
실제 응답을 받은 뒤 문장 검사/잘림으로 실패해도 모델·토큰·비용·시간은 전달한다. 호출 오류/timeout은 실제 응답 메타데이터를 만들지 않는다.

API 소비자인 서버의 nullable 비용/메타데이터 수용을 먼저 반영하고 AI를 뒤에 반영한다. 이전 서버는 null 비용을 거부한다.
실제 배포·유료 호출·키 설정은 별도 승인 범위다. Spring 공개 K4 필드는 이번 작업으로 바뀌지 않는다.

기존 완료 행의 0원·누락값은 다시 측정한 값으로 바꾸지 않는다. 과거 시험이나 FakeClient의 추정 토큰도 실측에 합산하지 않는다.
benchmark/evaluation 보고서는 관측 토큰이 누락된 합계·평균을 null로 처리하며 미확인을 0으로 더하지 않는다.

## 검증 방법

`pytest`의 insight_llm/service/contract/scenarios와 관련 집계 단위검사를 사용한다. 제공사 응답을 fixture로 대체하며 유료 호출은 없다.
실제 Spring HTTP 연동은 `python -m tests.insight_history_fixture`로 127.0.0.1:18033에서 fixture를 제공한다.
실제 prompt 조립·문장 검사·응답 직렬화는 실행하고 제공사만 고정 관측(모델 server33-provider-fixture, 토큰123/45, 비용0.000046)으로 대체한다.
Spring의 IPGO_INSIGHT_HISTORY_LOCAL_FASTAPI=true 검사가 이를 읽어 저장·조회한다. fixture를 실제 토큰/비용/문장 품질 측정으로 보고하지 않는다.

[Server #33](https://github.com/IpgoITGO/ipgo-server/issues/33)의 개발 환경 실제 유료 호출·전체 보존 조건 검증은 이 로컬 검사와 구분한다.

## 2026-11-06 로컬 검증 결과

Python 3.14.7·프로젝트 고정 의존성의 새 `.venv`에서 관련 6개 test module 134개 통과, 변경 Python 파일 ruff 통과.
성공/문장 검사 실패/호출 오류/timeout·usage 미수신/관측된 0·모델 미수신·누락 집계를 확인했다.
첫 검사에서 기존 0원 가정 4건이 실패했고 nullable 계약으로 보완했다. 다른 작업의 환경을 읽어 실행한 확장 검사는 PyYAML 누락으로 수집에 실패했다.
이번 작업의 독립 `.venv`에 고정 requirements-dev를 설치한 뒤 통과했으며 앞선 실패를 삭제하거나 실측 성공으로 합산하지 않는다.
실제 유료 LLM 호출·개발 배포는 이 결과에 포함하지 않는다.

SDK usage는 optional이며 미수신을 0으로 치환하지 않는 근거는 [OpenAI 공식 응답 형식](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)이다.
