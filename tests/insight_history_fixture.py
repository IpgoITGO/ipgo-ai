"""Server #33의 loopback HTTP 계약 검증 전용. 제공사·DB·AWS에 연결하지 않는다.

실행: python -m tests.insight_history_fixture
Spring의 IPGO_INSIGHT_HISTORY_LOCAL_FASTAPI=true 검사와 함께 사용한다.
고정 usage는 합성 표본이며 실제 비용·토큰 측정으로 사용하지 않는다.
"""
import json

import uvicorn

from app.main import app
from app.routers.insight import get_llm_client
from insight.llm import LLMResult


class ProviderFixture:
    model = "server33-provider-fixture"

    def complete(self, system, user):
        draft = json.dumps({"sentences": [
            "권장 입고량 {{qty_p100}}개를 넣으면 도착 후 품절 위험은 {{risk_basis}} 추정치로 {{risk_post_p100}}입니다.",
            "새 물량이 도착하기 전까지의 품절 위험은 수량과 관계없이 {{risk_pre}}입니다.",
        ]}, ensure_ascii=False)
        return LLMResult(draft, self.model, 123, 45, 17, 0.000046, False)


if __name__ == "__main__":
    app.dependency_overrides[get_llm_client] = ProviderFixture
    uvicorn.run(app, host="127.0.0.1", port=18033, lifespan="off", access_log=False)
