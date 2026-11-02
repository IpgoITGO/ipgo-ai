"""기본 문장 시험용 기준 구현 (#44). 운영 코드가 아니다: 기본 문장 생성 · 대체는 Spring이 한다 (#24).

#4 문서 6장 template으로 상태별 기대 문장을 만들어 Spring 구현(IpgoITGO/ipgo-server#61)과 대조할 기준으로 쓴다.
참조 구현 ai_insight의 fallback.py · placeholders.py를 0002 이름(#4 문서 5장)과 6장 조각으로 옮겼다.

- 조각에 필요한 키가 없으면 None: 문장 없이 비교표만 (#44 시나리오 13, template_error).
- 문장 모양은 #44 U4가 정해질 때까지 조각마다 한 문장인 sentences와 이어 붙인 text 둘 다 준다.
- tests/ 안에 있어 배포 이미지에 들어가지 않는다 (.dockerignore).
"""
import math
import re
from dataclasses import dataclass
from datetime import date

from schemas.base import Method, ResultStatus, RiskStatus, ScenarioType
from schemas.engine import ProductResult, ScenarioResult

PLACEHOLDER_RE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")

# 6장 조각. _X는 시나리오 접미사(p80 · p120 · custom)로 바꾼다.
PIECES = {
    ResultStatus.NOT_CALCULABLE: "이 상품은 권장 입고량을 계산하지 못했습니다. 사유: {{reason}}.",
    ResultStatus.EXCLUDED: "이 상품은 재고 기록 검산이 맞지 않아 이번 계산에서 제외했습니다. 사유: {{reason}}.",
    ResultStatus.REUPLOAD_REQUIRED: "이 상품은 수불부를 다시 올려야 계산할 수 있습니다. 사유: {{reason}}.",
    "not_needed": "{{method}}로 예측한 커버 기간 {{horizon_days}}일의 예상 출고량은 {{forecast_outbound}}개이고, "
                  "현재 재고 {{start_stock}}개와 도착 예정 물량 {{planned_receipts}}개로 충당되어 "
                  "이번에는 입고가 필요하지 않습니다.",
    "basis": "{{method}}({{formula_desc}})로 커버 기간 {{horizon_days}}일 동안 {{forecast_outbound}}개가 팔릴 것으로 "
             "예측하고 여유 배수 {{multiplier}}를 적용한 뒤, 현재 재고 {{start_stock}}개와 도착 예정 물량 "
             "{{planned_receipts}}개를 뺀 권장 입고량은 {{qty_p100}}개입니다.",
    "method_reason": "이 산식을 쓴 이유: {{method_reason}}.",
    "start_estimated": "현재 재고 {{start_stock}}개는 기준일 {{as_of_date}} 이후의 출고를 예측해 추정한 값입니다.",
    "p100_risk": "{{qty_p100}}개를 넣으면 다음 입고 때 {{ending_p100}}개가 남고 도착 후 품절 위험은 {{risk_basis}} "
                 "추정치로 {{risk_post_p100}}입니다(표본 {{risk_sample_size}}건).",
    "p100": "{{qty_p100}}개를 넣으면 다음 입고 때 {{ending_p100}}개가 남습니다.",
    "insufficient_sample": "품절 위험은 과거 예측 표본이 부족해 산출하지 않았습니다.",
    "low_grade": "예측 기록이 짧아 품절 위험은 산출하지 않았고, 대신 예상 출고량 범위를 보여 드립니다: "
                 "{{forecast_range_low}}~{{forecast_range_high}}개.",
    "other_risk": "{{qty_X}}개로 {{action_X}} 다음 입고 때 남는 재고는 {{ending_X}}개로 {{dir_ending_X_connect}} "
                  "품절 위험은 {{risk_post_X}}로 {{dir_risk_post_X_end}}.",
    "other": "{{qty_X}}개로 {{action_X}} 다음 입고 때 남는 재고는 {{ending_X}}개로 {{dir_ending_X_end}}.",
    "risk_pre": "새 물량이 도착하기 전까지의 품절 위험은 수량과 관계없이 {{risk_pre}}입니다.",
}

# 문구 키(†)의 값. Spring이 같은 표를 쓴다 (#4 문서 5장, 0002 3-6)
FORMULA_DESC = {Method.A: "작년 같은 시기 판매량에 올해 최근 흐름을 반영하는 방식",
                Method.B: "최근 유효 관측일 평균 판매량을 쓰는 방식"}
RISK_BASIS = {"SKU": "이 상품의 과거 오차 기준", "POOLED": "전체 상품의 과거 오차 기준"}
ACTION = {"lower": "줄이면", "higher": "늘리면", "same": "그대로 두면"}
DIR_ENDING = {"lower": ("줄지만", "줄어듭니다"), "higher": ("늘지만", "늘어납니다"), "same": ("같고", "같습니다")}
DIR_RISK = {"lower": ("낮아지고", "낮아집니다"), "higher": ("높아지고", "높아집니다"), "same": ("같고", "같습니다")}
OTHERS = (ScenarioType.P80, ScenarioType.P120, ScenarioType.CUSTOM)


@dataclass(frozen=True)
class Context:
    """ProductResult 밖에서 오는 값. 계산 요청(asOfDate)과 0002에서 더하기로 한 필드(스키마 반영 전, #10)."""
    as_of_date: date | None = None
    method_fallback: bool = False   # 0002 3-3 methodFallback
    risk_basis: str = "SKU"         # 0002 3-8 riskBasis (SKU · POOLED)


@dataclass(frozen=True)
class Fallback:
    template: list[str]   # 자리 표시자가 남은 조각 (Spring 구현과 대조)
    sentences: list[str]  # 값을 채운 문장 (#44 U4: 배열)

    @property
    def text(self) -> str:  # #44 U4: 하나의 글
        return " ".join(self.sentences)


def _int(x: float) -> str:
    """개수는 천 단위 쉼표. 소수는 반올림한다: 화면 정수 표시 규칙이 정해지면 바꾼다 (#4 문서 7장 소수 표시)."""
    return f"{math.floor(x + 0.5):,}"


def _pct(x: float) -> str:
    return f"{math.floor(x * 100 + 0.5)}%"


def _date(d: date) -> str:
    return f"{d.month}월 {d.day}일"


def _cmp(a: float, b: float) -> str:
    return "higher" if a > b else "lower" if a < b else "same"


def _key(s: ScenarioResult) -> str:
    return s.type.value.lower()


def placeholder_values(r: ProductResult, ctx: Context) -> dict[str, str]:
    """6장 조각에 들어가는 자리 표시자 값. 값이 없는 항목은 넣지 않는다 (#4 문서 5장)."""
    v: dict[str, str] = {}
    if r.reason:
        v["reason"] = r.reason.strip().rstrip(".")
    if r.method is not None:
        v["method"], v["formula_desc"] = f"계산식 {r.method.value}", FORMULA_DESC[r.method]
    if ctx.method_fallback and r.method_reason:
        v["method_reason"] = r.method_reason.strip().rstrip(".")
    for key, x in (("horizon_days", r.horizon_days), ("forecast_outbound", r.forecast_outbound_qty),
                   ("start_stock", r.start_stock_qty), ("planned_receipts", r.planned_receipts_in_horizon)):
        if x is not None:
            v[key] = _int(x)
    if r.multiplier is not None:
        v["multiplier"] = _pct(r.multiplier)
    if ctx.as_of_date and r.start_stock_estimated:
        v["as_of_date"] = _date(ctx.as_of_date)

    risk = r.risk_status == RiskStatus.OK
    if risk:
        if r.pre_arrival_risk is not None:
            v["risk_pre"] = _pct(r.pre_arrival_risk)
        if r.risk_sample_size is not None:
            v["risk_sample_size"] = _int(r.risk_sample_size)
        v["risk_basis"] = RISK_BASIS[ctx.risk_basis]
    if r.risk_status == RiskStatus.LOW_GRADE and r.forecast_range_low is not None and r.forecast_range_high is not None:
        v["forecast_range_low"], v["forecast_range_high"] = _int(r.forecast_range_low), _int(r.forecast_range_high)

    described = [s for s in r.scenarios if not s.is_duplicate]
    base = next((s for s in described if s.type == ScenarioType.P100), None)
    for s in described:
        x = _key(s)
        v[f"qty_{x}"], v[f"ending_{x}"] = _int(s.qty), _int(s.expected_ending_stock)
        if risk and s.post_arrival_risk is not None:
            v[f"risk_post_{x}"] = _pct(s.post_arrival_risk)
        if base is None or s is base:
            continue
        v[f"action_{x}"] = ACTION[_cmp(s.qty, base.qty)]
        v[f"dir_ending_{x}_connect"], v[f"dir_ending_{x}_end"] = DIR_ENDING[
            _cmp(s.expected_ending_stock, base.expected_ending_stock)]
        if risk and s.post_arrival_risk is not None and base.post_arrival_risk is not None:
            v[f"dir_risk_post_{x}_connect"], v[f"dir_risk_post_{x}_end"] = DIR_RISK[
                _cmp(s.post_arrival_risk, base.post_arrival_risk)]
    return v


def fallback_template(r: ProductResult, ctx: Context) -> list[str]:
    """상태에 맞는 6장 조각을 표 순서대로 고른다. 조각 하나가 문장 하나다."""
    if r.status in (ResultStatus.NOT_CALCULABLE, ResultStatus.EXCLUDED, ResultStatus.REUPLOAD_REQUIRED):
        return [PIECES[r.status]]
    if r.status == ResultStatus.NOT_NEEDED:
        return [PIECES["not_needed"]] + ([PIECES["start_estimated"]] if r.start_stock_estimated else [])

    risk = r.risk_status == RiskStatus.OK
    t = [PIECES["basis"]]
    if ctx.method_fallback:
        t.append(PIECES["method_reason"])
    if r.start_stock_estimated:
        t.append(PIECES["start_estimated"])
    t.append(PIECES["p100_risk" if risk else "p100"])
    if r.risk_status == RiskStatus.INSUFFICIENT_SAMPLE:
        t.append(PIECES["insufficient_sample"])
    elif r.risk_status == RiskStatus.LOW_GRADE:
        t.append(PIECES["low_grade"])
    for s in sorted((s for s in r.scenarios if s.type in OTHERS and not s.is_duplicate),
                    key=lambda s: OTHERS.index(s.type)):
        t.append(PIECES["other_risk" if risk else "other"].replace("_X", f"_{_key(s)}"))
    if risk:
        t.append(PIECES["risk_pre"])
    return t


def fill(template: str, values: dict[str, str]) -> str:
    """자리 표시자를 값으로 바꾼다. 값이 없는 키가 있으면 KeyError."""
    return PLACEHOLDER_RE.sub(lambda m: values[m[1]], template)


def build_fallback(r: ProductResult, ctx: Context | None = None) -> Fallback | None:
    """상태별 기본 문장. 필요한 키가 하나라도 없으면 None (비교표만)."""
    ctx = ctx or Context()
    template = fallback_template(r, ctx)
    values = placeholder_values(r, ctx)
    try:
        return Fallback(template, [fill(t, values) for t in template])
    except KeyError:
        return None
