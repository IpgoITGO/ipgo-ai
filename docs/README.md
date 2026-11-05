# 개발 문서 안내

필요한 문서만 선택해 읽습니다. 공통 규칙은 한곳에만 기록하고 다른 문서에서 링크로 연결합니다.

| 확인할 내용 | 문서 |
|---|---|
| Issue부터 병합까지의 순서 | [작업 흐름](rules/workflow.md) |
| Issue·PR·commit 작성 방식 | [작성 규칙](rules/conventions.md) |
| 비밀값과 위험한 변경 기준 | [안전 규칙](rules/safety.md) |
| 폴더 구조와 Spring 연동 | [구조와 연동](architecture.md) |
| 연동 계약 결정 기록 | [decisions](decisions/) |
| DA-08 최소 시계열 오차 표본 | [입력·오차·제외·검증 범위](decisions/0005-da08-error-samples.md) |
| DA-07 품절 위험 | [표본 선택·시나리오 재적용·미확정 정책](decisions/0006-da07-stockout-risk.md) |
| DA-07 최소 기준·HTTP/SQS | [최소20건·주간 정책·원본 분석과 공급](decisions/0007-da07-runtime-sampling.md) |
| Server76 PostgreSQL 원본 조회와 적용 순서 | [DB 원본 계약](decisions/0005-database-original.md) |
| AI 인사이트 prompt·자리 표시자·기본 문장 | [prompt 규칙과 기본 문장](ai-insight/prompt-and-fallback.md) |
| AI 인사이트 LLM 모델 선정·측정 결과 | [LLM 모델 선정](ai-insight/llm-model-selection.md) |

AI 실행 방법과 주요 구조는 루트 [README](../README.md)에 유지합니다.
