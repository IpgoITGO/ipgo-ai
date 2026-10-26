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

## 세부 문서

| 작업 | 문서 |
|---|---|
| branch·Issue·PR 흐름 | [작업 흐름](docs/rules/workflow.md) |
| 문장·commit·Markdown 형식 | [작성 규칙](docs/rules/conventions.md) |
| 비밀값·dataset·외부 영향 | [안전 규칙](docs/rules/safety.md) |
