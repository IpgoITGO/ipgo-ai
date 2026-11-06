# 지속 LLM의 요청 제한

2026-11-06 개발환경 지속 사용·월 API US$10 요청에 맞춘 AI 측 경계다. 월 카운터는 Server V22에서 전체 회원 합계로 보호하며, AI 쪽 요청만 단독 공개하거나 다른 호출자가 같은 키로 요청하는 경로까지 계정 청구 상한을 보장하지 않는다.

`IPGO_LLM_CONTINUOUS_ENABLED=true`이면 만료시각이 비어도 SDK 직전에 시연 모드와 같은 snapshot/Standard/n1·메시지 UTF-8 bytes+1024≤16,000·출력≤400·공식 origin 제한을 검사한다. 명시 expiry가 있으면 지속 승인으로 우회하지 않는다. SDK 재시도와 HTTP redirect를 끈다. 월 정책의 Server 승인을 대체하지 않는다.

지속 모드는 workflow의 별도 승인과 LLM/key 승인이 함께 있어야 한다. 기본은OFF다. Server 월 guard와 V22를 먼저 준비하고 AI를 적용한 뒤 Server의 신규 생성/worker를 켠다. 기존 재생성 단건·배치 범위는 자동 확대하지 않는다. 중단은 Server gate OFF와 key 제거이며 이미 시작된 공급자 요청의 청구 취소를 보장하지 않는다.

검증은 가짜 SDK와 실제 OpenAI SDK client를 사용한 경계 검사이며 실제 모델/청구 검증과 구분한다. [공식 모델](https://developers.openai.com/api/docs/models/gpt-4o-mini)과 [공식 가격](https://developers.openai.com/api/docs/pricing)을 2026-11-06 확인했다. 기존 미커밋 `insight-cost-policy` worktree는 수정하거나 이 변경에 섞지 않았다.
