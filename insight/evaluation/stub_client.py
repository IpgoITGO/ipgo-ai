"""API 키 없이 평가 파이프라인을 점검하는 가짜 LLM (insight.llm.LLMClient 규약). 참조 구현 evaluation/stub_client.py를
자리 표시자 이름만 0002로 바꿔 옮겼다 (이름표: tests/fixtures/insight_prompts.md, #4 문서 5장).

프롬프트의 자리 표시자 목록을 읽어, 그 결과에 실제로 있는 키만으로 초안을 만든다.
일부러 규칙 위반 초안을 섞어(bad_rate) 기본 문장 대체 경로도 시험한다. 판정 근거로 쓰면 안 된다.
"""

import hashlib
import json
import re

from insight.llm import LLMResult

_KEY_RE = re.compile(r"^\s*\{\{([a-z][a-z0-9_]*)\}\} —", re.M)


class StubClient:
    model = "stub"

    def __init__(self, bad_rate: float = 0.05):
        self.bad_rate = bad_rate
        self.n = 0

    def complete(self, system: str, user: str, *, max_tokens: int = 400, temperature: float = 0.2) -> LLMResult:
        self.n += 1
        keys = set(_KEY_RE.findall(user))
        h = int(hashlib.sha256(f"{user}:{self.n}".encode()).hexdigest(), 16)
        if (h % 1000) / 1000 < self.bad_rate:
            sents = ["권장 입고량 {{qty_p100}}개가 가장 적절합니다.", "재고가 약 30% 늘어납니다."]
        elif "qty_p100" not in keys:  # 입고 불필요 + 직접 입력만 있는 건
            sents = [
                "{{method}}로 예측한 커버 기간 {{horizon_days}}일의 예상 출고량은 "
                "{{forecast_outbound}}개이고 현재 재고 {{start_stock}}개와 도착 예정 물량 "
                "{{planned_receipts}}개로 충당되어 이번에는 입고가 필요하지 않습니다."
            ]
            if "qty_custom" in keys:
                sents.append("직접 입력한 {{qty_custom}}개를 넣으면 다음 입고 때 {{ending_custom}}개가 남습니다.")
            if "risk_pre" in keys:
                sents.append("새 물량이 도착하기 전까지의 품절 위험은 {{risk_basis}} 추정치로 {{risk_pre}}입니다.")
        else:
            # 참조 구현은 첫 문장에 산식 근거 문장을 두어 4문장이 되기도 한다. 저장소 규칙은 2~3문장
            # (#4 문서 3장, insight/validator.py)이고 system prompt "문장 구성 지침"이
            # 1. 권장량 2. 시나리오 비교 3. 공통 위험이라 근거 문장은 뺐다.
            sents = []
            if "risk_post_p100" in keys:
                sents.append(
                    "{{qty_p100}}개를 넣으면 다음 입고 때 {{ending_p100}}개가 남고 도착 후 품절 위험은 "
                    "{{risk_basis}} 추정치로 {{risk_post_p100}}입니다."
                )
            else:
                sents.append("{{qty_p100}}개를 넣으면 다음 입고 때 {{ending_p100}}개가 남습니다.")
            alt = ["p120", "p80"] if h % 2 else ["p80", "p120"]
            for k in alt:
                if f"dir_risk_post_{k}_end" in keys:
                    sents.append(
                        f"{{{{qty_{k}}}}}개로 {{{{action_{k}}}}} 남는 재고는 {{{{ending_{k}}}}}개로 "
                        f"{{{{dir_ending_{k}_connect}}}} "
                        f"품절 위험은 {{{{risk_post_{k}}}}}로 {{{{dir_risk_post_{k}_end}}}}."
                    )
                    break
                if f"dir_ending_{k}_end" in keys:
                    sents.append(
                        f"{{{{qty_{k}}}}}개로 {{{{action_{k}}}}} 남는 재고는 {{{{ending_{k}}}}}개로 "
                        f"{{{{dir_ending_{k}_end}}}}."
                    )
                    break
            if "risk_pre" in keys:
                sents.append("새 물량이 도착하기 전까지의 품절 위험은 수량과 관계없이 {{risk_pre}}입니다.")
        text = json.dumps({"sentences": sents}, ensure_ascii=False)
        return LLMResult(
            text, self.model, len(system) // 4 + len(user) // 4, len(text) // 3, 900 + h % 900, None, False
        )
