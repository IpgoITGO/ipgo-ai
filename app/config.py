"""환경 변수 설정. 값은 .env에 두고, 이름은 .env.example에 적는다."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    engine_mode: str          # "mock": 명세 예시 응답을 돌려줌 · "real": 실제 계산
    database_url: str | None  # 계산 엔진은 읽기 전용 (DB 방식 미정 → docs/decisions/0001)
    storage_base: str | None  # 원본 엑셀 위치 (미정 → 설정 전에는 STORAGE_NOT_CONFIGURED)


def get_settings() -> Settings:
    """요청마다 읽는다 (테스트에서 환경 변수를 바꿔 끼울 수 있게)."""
    return Settings(
        engine_mode=os.getenv("ENGINE_MODE", "mock"),
        database_url=os.getenv("DATABASE_URL") or None,
        storage_base=os.getenv("STORAGE_BASE") or None,
    )
