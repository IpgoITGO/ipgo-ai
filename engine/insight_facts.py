"""DA-11 → AI-01 연결: 계산 결과를 AI 인사이트 입력(자리 표시자 · 비교 방향)으로 바꾼다."""
from schemas.engine import ProductResult
from schemas.insight import InsightInput


def to_insight_input(result: ProductResult, sku_label: str) -> InsightInput:
    """예: risk_post_p80 > risk_post_p100 이면 directions["risk_post_p80_vs_p100"] = "높음"."""
    raise NotImplementedError("DA-11 to_insight_input")
