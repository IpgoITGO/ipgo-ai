# Ipgo AI 개발 지침

- 적용 범위: 이 저장소 전체
- 작업 시작 전 연결된 Issue의 목적과 완료 조건을 확인합니다.
- 한 Issue와 PR에는 하나의 작업 목적만 포함합니다.

## 개발 흐름

- 최신 `dev`에서 `feature/<issue-number>-<description>` 브랜치를 만듭니다.
- 구현과 검증 후 `dev` 대상 PR을 생성합니다.
- `main` 변경은 `dev → main` PR로만 진행합니다.
- `main`에 직접 push하거나 force push하지 않습니다.
- `main` PR은 작성자 외 팀원 1명의 승인을 받아야 합니다.

## 작성과 검증

- Issue와 PR은 쉬운 한국어로 작성하고 기술 용어는 자연스러운 영어 표기를 유지합니다.
- commit은 `type: short English summary` 형식을 사용합니다.
- PR에는 상세 변경, 충돌 위험도, model·dataset·inference 영향과 실제 검증 결과를 기록합니다.
- 모든 작업에서 README를 확인하고 동작·구조·실행 방법이 바뀌면 같은 PR에서 갱신합니다.
- 변경 범위에 맞는 검사를 실행하고 재현 조건과 결과를 PR에 남깁니다.

## 안전

- 비밀값, 인증정보, 개인정보, 원본 dataset과 model weight를 임의로 commit하지 않습니다.
- 기존 사용자 변경과 현재 Issue에 무관한 변경을 섞지 않습니다.
- 외부 전송, 대용량 데이터 변경, 배포와 비용 발생 작업은 실행 전 영향과 복구 방법을 확인합니다.

## 구조와 담당

- `engine/`은 계산 엔진(장준영), `insight/`는 AI 인사이트(이석형) 담당입니다. 다른 담당 폴더는 제안만 하고 직접 수정하지 않습니다.
- `app/main.py`는 라우터 연결만 합니다. 엔드포인트는 `app/routers/`의 각자 파일에 씁니다.
- `engine/`은 순수 파이썬 함수만 둡니다. fastapi · openai · DB 드라이버를 import하지 않습니다.
- FastAPI는 DB를 읽기만 합니다. 쓰기는 Spring Boot가 합니다.
- `schemas/`는 Spring과의 계약입니다. 필드는 명세 → `schemas/` → Spring 순서로 함께 바꾸고 `docs/decisions/`에 남깁니다.

## 계산과 테스트

- `tests/`의 기대값과 `tests/fixtures/`는 사람이 손계산으로 정합니다. 테스트가 실패하면 구현을 고치고, 기대값이 틀렸다고 보이면 멈추고 이유를 설명합니다.
- 계산 불가는 0개로 바꾸지 않고 `NOT_CALCULABLE`과 `reason`으로 돌려줍니다.
- 백테스트에서 기준일 t의 예측에는 t일까지의 정보만 씁니다.
- 엔진은 소수로 계산하고, 정수는 입고 단위로 올린 수량과 금액뿐입니다.
- 같은 입력이면 같은 결과가 나와야 합니다. 엔진 안에서 현재 시각이나 난수를 쓰지 않고 `today`는 요청으로 받습니다.
- LLM에는 해당 SKU의 구조화 결과만 보냅니다. 원본 수불부 · 원가 · 계정 정보 · 다른 SKU는 보내지 않습니다.
- 실제 셀러 수불부는 public 저장소에 올리지 않습니다. 테스트에는 직접 만든 가짜 데이터만 씁니다.

## 세부 문서

| 작업 | 문서 |
|---|---|
| branch·Issue·PR 흐름 | [작업 흐름](docs/rules/workflow.md) |
| 문장·commit·Markdown 형식 | [작성 규칙](docs/rules/conventions.md) |
| 비밀값·dataset·외부 영향 | [안전 규칙](docs/rules/safety.md) |
| 폴더 구조·연동 계약 | [구조와 연동](docs/architecture.md) |
| 계약 결정 기록 | [docs/decisions](docs/decisions/) |
