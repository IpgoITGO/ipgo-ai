# AI-01 모델 인사이트 수용 기록

입력은 비공개 고정 `lgbm19-60a4d15301cd5490` weight와 직접 만든 수불부다.
실제 셀러 자료·상품 대응값·원가는 LLM에 보내지 않았다. 공개 고정본은
`tests/fixtures/model_insight_inputs.json`의 이름 목록·방향·플래그뿐이다.

## 실제 모델과 유료 LLM

- 실제 weight + 합성100일 수불부: 양수 권장량, P80 중복, 추정 재고+날짜 없는 발주,
  도착 예정 발주, 현재 재고, NOT_NEEDED, 미지원 NOT_CALCULABLE의7경계를 확인했다.
- 앞5유형마다 실제 GPT-4o mini6회, 총30회 호출했다. 자동 재호출0이다.
- 모델 prompt v0.2: 규칙·필수 키·모델 위험 사유 검사30/30, placeholder 밖 숫자0,
  A/B 표본 부족·산식·배수·확률 오설명0이다. 원본 응답·토큰·시간은 비공개 원장에 보존했다.
- 실제 provider usage의 단가 환산 합은 $0.010758이다. 실제 청구액 관측이 아니며
  사전에 계산한 UTF-8 byte 기반 보수적 상계 $0.0381708과 $0.10 한도 이내였다.
- 이 수치는 실제 weight + 합성 입력의 실제 LLM 결과다. 고객 실사용 성과·Spring 값 채우기·
  새 비동기 작업·브라우저 완료로 확대하지 않는다. LLM 판사 평가를 수행한 것도 아니다.

## 연결과 기본 문장

계약·OFF 스위치·연결 순서는 [결정0018](../decisions/0018-model-insight-contract.md),
기본 문장은 [prompt 기준](prompt-and-fallback.md)을 따른다. 기존 A/B prompt/version은 보존한다.
Server253과 함께 actual API·DB·화면 및 오류 대체를 검증한 결과는 AI44의 시나리오별 원장에 연결한다.
개발 환경의 모델 설명은 계속 OFF다. 실제 활성화와 구현·격리 시험을 구분한다.
