"""효과 평가 설정 (insight/evaluation/eval.yaml).
참조 구현(ai_insight v0.3) evaluation/config.py에서 1단계에 쓰는 절만 옮겼다.

LLM · 검사 규칙은 서비스 설정(.env의 OPENAI_MODEL · LLM_TIMEOUT_SEC, insight/validator.py)을 그대로 쓴다.
2단계(채택 규칙 adoption_rule · 시뮬레이션 simulation · 사전 등록 rule_fingerprint)는 #54 제외 범위라 옮기지 않았다.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel


class ExperimentConfig(BaseModel):
    n_sets: int = 3
    scenarios_per_set: int = 4
    practice_trials: int = 1


class OfflineGate(BaseModel):
    min_validation_pass_rate: float = 0.9  # LLM 호출 건 중 path=llm 비율
    max_fill_mismatch: int = 0  # 최종 표시 문장 중 number_check_ok=False 건수


class MockConfig(BaseModel):
    low_grade_rate: float = 0.0  # 가짜 결과 중 LOW_GRADE 비율 (#54 확인 필요, DA-13 전이라 기본 0)


class JudgeConfig(BaseModel):
    """LLM 판사 (judge.py). 참고 지표라 관문에 넣지 않는다."""

    model: str = "gpt-5.6-sol"  # #54 판사 모델. 환경변수 JUDGE_MODEL이 우선
    runs: int = 2  # 같은 문장을 몇 번 판정할지. 모두 같을 때만 확정
    max_tokens: int = 1200  # max_completion_tokens로 보냄(추론 토큰 포함, #54 11/3 시험 호출)
    timeout_sec: float = 60
    scope: str = "experiment"  # experiment | offline | all
    # C4 근거 수치. 참조 구현 cover_demand · multiplier · current_stock · pending_qty · cover_days의 0002 이름
    basis_keys: list[str] = ["forecast_outbound", "multiplier", "start_stock", "planned_receipts", "horizon_days"]
    advisory_min_pass_rate: float = 0.9  # 보고서 표시용 참고 기준 (관문 아님)


class Config(BaseModel):
    experiment: ExperimentConfig = ExperimentConfig()
    offline_gate: OfflineGate = OfflineGate()
    mock: MockConfig = MockConfig()
    judge: JudgeConfig = JudgeConfig()


DEFAULT_PATH = Path(__file__).with_name("eval.yaml")


def load_config(path: str | Path = DEFAULT_PATH) -> Config:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    return Config.model_validate(data or {})
