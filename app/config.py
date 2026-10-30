"""환경 변수 설정. 값은 .env에 두고, 이름은 .env.example에 적는다."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    engine_mode: str
    database_url: str | None
    storage_base: str | None
    aws_region: str
    upload_bucket: str | None
    jobs_queue_url: str | None
    results_queue_url: str | None


def get_settings() -> Settings:
    """요청마다 읽는다 (테스트에서 환경 변수를 바꿔 끼울 수 있게)."""
    return Settings(
        engine_mode=os.getenv("ENGINE_MODE", "mock"),
        database_url=os.getenv("DATABASE_URL") or None,
        storage_base=os.getenv("STORAGE_BASE") or None,
        aws_region=os.getenv("AWS_REGION", "ap-northeast-2"),
        upload_bucket=os.getenv("IPGO_UPLOAD_BUCKET") or None,
        jobs_queue_url=os.getenv("IPGO_JOBS_QUEUE_URL") or None,
        results_queue_url=os.getenv("IPGO_RESULTS_QUEUE_URL") or None,
    )
