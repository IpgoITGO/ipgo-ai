"""5~7월 자료 확인 후 선택한 정책을 배포 코드와 함께 고정한다."""
import hashlib
import json
import re
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.errors import AppError
from engine.risk_sampling import RiskSamplingPolicy
from engine.scenario import RiskInputError

MAX_POLICY_BYTES = 16_384
_FIELDS = {"version", "periodStart", "periodEnd", "availableOn", "origins", "minSamples"}
DEFAULT_POLICY_SHA256 = "a84a6940c8e2cfe008b5b034280146661b5bb87f7e683f77ac5f337af450eb8a"
DEFAULT_POLICY_PATH = Path(__file__).with_name("risk_policy_2026.json")


def load_risk_policy() -> RiskSamplingPolicy | None:
    """비활성은 None, 기본은 고정 정책이다. 파일/해시가 손상됐으면 503이다."""
    settings = get_settings()
    if not settings.risk_enabled:
        return None
    path, digest = DEFAULT_POLICY_PATH, DEFAULT_POLICY_SHA256
    try:
        if not path or not digest or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("정책 고정 정보 누락")
        # 큰 파일을 읽은 뒤 검사하지 않는다. 정책에는 원본·상품별 값이 필요 없다.
        with Path(path).open("rb") as stream:
            content = stream.read(MAX_POLICY_BYTES + 1)
        if len(content) > MAX_POLICY_BYTES or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("정책 크기/해시 불일치")
        def unique_fields(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("중복 정책 항목")
                obj[key] = value
            return obj
        obj = json.loads(content, object_pairs_hook=unique_fields)
        if not isinstance(obj, dict) or set(obj) != _FIELDS or not isinstance(obj["origins"], list):
            raise ValueError("정책 구조 불일치")
        return RiskSamplingPolicy(
            version=obj["version"], period_start=date.fromisoformat(obj["periodStart"]),
            period_end=date.fromisoformat(obj["periodEnd"]), available_on=date.fromisoformat(obj["availableOn"]),
            origins=tuple(date.fromisoformat(d) for d in obj["origins"]), min_samples=obj["minSamples"],
        )
    except (OSError, UnicodeError, ValueError, TypeError, RiskInputError) as exc:
        raise AppError(503, "RISK_POLICY_INVALID", "위험 계산 정책 설정을 확인해 주세요") from exc
