"""AI-01 · AI-02 문장 초안 검사 (#29). 규칙과 사유 코드는 docs/ai-insight/prompt-and-fallback.md 4장을 따른다.

LLM 응답 JSON 해석, 자리 표시자 목록 만들기, 값 넣기는 여기서 하지 않는다 (#29 제외 범위).
"""
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# 아직 정해지지 않은 값은 여기서만 바꾼다 (#29)
PLACEHOLDER_RE = re.compile(r"\{\{([a-z][a-z0-9_.]*)\}\}")  # 명세는 { } (IpgoITGO/ipgo-server#25)
MIN_SENTENCES, MAX_SENTENCES = 2, 3  # #4 문서 3장. 참조 구현은 2~5 (7장)
RISK_KEY_PREFIX = "risk_"  # 지금 문서 이름(risk_after.s100)과 0002 3-4 이름(risk_post_p100) 모두 risk_로 시작
RISK_BASIS_KEY = "risk_basis"

DIGIT_RE = re.compile(r"[0-9０-９%％]")
KOREAN_NUMERALS = [
    r"[한두세네]\s?(배|개|달|주|번째)", r"다섯\s?(배|개)", r"절반", r"반\s?정도",
    r"열흘", r"하루", r"이틀", r"사흘", r"나흘", r"닷새", r"보름", r"일주일",
    r"[일이삼사오육칠팔구십백천만]{1,3}\s?(개|일|주|%|퍼센트|배)",
]
# 비교 결과의 방향. 셀러의 행동(늘리면 · 줄이면)은 허용한다.
DIRECTION_WORDS = [
    "높아", "낮아", "늘어", "줄어", "늘지만", "줄지만", "증가", "감소", "커지", "작아지", "많아", "적어",
    "더 많", "더 적", "더 높", "더 낮", "높은", "낮은", "높다", "낮다", "높습니다", "낮습니다",
    "낮춰", "올라가", "올라갑",  # #4 문서 4장 예 (7장: 참조 구현에 없던 말)
]
EVAL_WORDS = [
    "최적", "최선", "최고", "가장 좋", "가장 나", "적절", "추천", "권합니다", "권장합니다", "권해", "제안합니다",
    "반드시", "무조건", "확실히", "틀림없이", "분명히", "꼭 ", "안전합니다", "안전한", "안전하",
    "위험합니다", "위험한 선택", "충분합니다", "충분한", "부족합니다", "부족한", "바람직", "유리", "불리",
    "좋습니다", "좋은 선택", "나쁜", "과도", "지나치", "합리적", "현명", "무난", "괜찮", "낫습니다", "나은",
    "품절됩니다",
]
POLICY_PATTERNS = [
    r"(배수|계산식|계산 방식|산식|권장량|권장 수량|리드타임|커버 기간)[을를은는이가]?\s*"
    r"(바꾸|변경|조정|수정|올리|내리|낮추|높이|다시 계산|재계산)",
    r"개\s*(으로|로)\s*(권장|추천|제안|변경)",  # "300개로 권장". "계산식 A로 권장 입고량은"은 허용
]
MARKDOWN_PATTERNS = [r"^\s*[-*#>]", r"\*\*", r"`", r"\|"]


@dataclass
class Violation:
    code: str  # raw_number | unknown_key | missing_key | banned_word | format
    detail: str


@dataclass
class CheckResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def codes(self) -> set[str]:
        return {v.code for v in self.violations}


def _first_match(patterns: list[str], s: str) -> re.Match | None:
    return next((m for p in patterns if (m := re.search(p, s))), None)


def check_draft(text: str, allowed: Iterable[str], required: Iterable[str] = ()) -> CheckResult:
    """문장 초안을 검사한다. allowed는 쓸 수 있는 자리 표시자, required는 꼭 써야 하는 자리 표시자."""
    v: list[Violation] = []
    used = set(PLACEHOLDER_RE.findall(text))
    body = PLACEHOLDER_RE.sub(" ", text)  # 자리 표시자 밖의 글자만

    if m := DIGIT_RE.search(body):
        v.append(Violation("raw_number", f"자리 표시자 밖 숫자 '{body[max(0, m.start() - 6):m.end() + 6].strip()}'"))
    elif m := _first_match(KOREAN_NUMERALS, body):
        v.append(Violation("raw_number", f"한글 수사 '{m.group(0)}'"))

    v += [Violation("unknown_key", f"목록에 없는 자리 표시자 {{{{{k}}}}}") for k in sorted(used - set(allowed))]
    v += [Violation("missing_key", f"필수 자리 표시자 {{{{{k}}}}} 누락") for k in required if k not in used]

    if hit := [w for w in DIRECTION_WORDS if w in body]:
        v.append(Violation("banned_word", f"방향 어휘 {hit[:4]}"))
    if hit := [w for w in EVAL_WORDS if w in body]:
        v.append(Violation("banned_word", f"평가 · 추천 · 확정 어휘 {hit[:4]}"))
    if m := _first_match(POLICY_PATTERNS, text):
        v.append(Violation("banned_word", f"계산 방식 · 권장량 변경 표현 '{m.group(0)}'"))
    if any(k.startswith(RISK_KEY_PREFIX) for k in used) and RISK_BASIS_KEY not in used and "추정" not in body:
        v.append(Violation("banned_word", f"위험을 쓰면서 '추정'이나 {{{{{RISK_BASIS_KEY}}}}}가 없음"))

    if "\n" in text or any(re.search(p, text, flags=re.M) for p in MARKDOWN_PATTERNS):
        v.append(Violation("format", "줄바꿈 · 마크다운"))
    n = len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()])
    if not MIN_SENTENCES <= n <= MAX_SENTENCES:
        v.append(Violation("format", f"문장 수 {n} (허용 {MIN_SENTENCES}~{MAX_SENTENCES})"))
    if "{" in body or "}" in body:
        v.append(Violation("format", "자리 표시자 형식이 아닌 중괄호"))

    return CheckResult(v)
