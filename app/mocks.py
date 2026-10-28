"""ENGINE_MODE=mock일 때 돌려줄 응답. 값은 API 명세서 예시를 따른다."""
import json
from functools import cache
from pathlib import Path

from pydantic import BaseModel

MOCK_DIR = Path(__file__).parent / "mock_data"


@cache
def load_mock[M: BaseModel](name: str, model: type[M]) -> M:
    """mock_data/<name>.json을 읽어 스키마로 검증한다. 예시가 스키마와 어긋나면 여기서 바로 실패한다."""
    data = json.loads((MOCK_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return model.model_validate(data)
