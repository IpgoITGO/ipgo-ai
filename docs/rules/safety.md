# 안전 규칙

- 공개 저장소에 token, 비밀번호, 개인정보, 비공개 dataset과 `.env` 내용을 기록하지 않습니다.
- 원본 dataset, 생성 데이터와 model weight는 명시된 저장 정책 없이 commit하지 않습니다.
- model, dataset, prompt 변경은 평가 기준과 재현 조건을 PR에 적습니다.
- inference 계약 변경은 server와 client 연동 호환성을 먼저 확인합니다.
- 외부 전송, 삭제, 배포와 비용이 발생하는 작업은 실행 전에 대상·영향·복구 방법을 확인합니다.
- 기존 변경을 임의로 되돌리거나 현재 Issue와 무관한 파일을 commit하지 않습니다.
