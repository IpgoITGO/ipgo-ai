"""Spring이 기록한 storage key로 원본 엑셀을 읽는다."""
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings
from app.errors import AppError
from engine.clean import CleaningInputError
from schemas.engine import SourceFile

MAX_SOURCE_BYTES = 10 * 1024 * 1024


def read_source_file(file: SourceFile) -> bytes:
    settings = get_settings()
    key = file.storage_key
    if not key or key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
        raise CleaningInputError("원본 파일 경로가 올바르지 않아요")
    object_key = f"{key}/original.xlsx"
    if settings.upload_bucket is not None:
        try:
            response = boto3.client("s3", region_name=settings.aws_region,
                                   config=Config(connect_timeout=3, read_timeout=10,
                                                 retries={"total_max_attempts": 2})).get_object(
                Bucket=settings.upload_bucket,
                Key=object_key,
            )
            if response.get("ContentLength", 0) > MAX_SOURCE_BYTES:
                raise CleaningInputError("원본 파일 크기 제한을 초과했어요")
            body = response["Body"]
            try:
                content = body.read(MAX_SOURCE_BYTES + 1)
            finally:
                if hasattr(body, "close"):
                    body.close()
            if len(content) > MAX_SOURCE_BYTES:
                raise CleaningInputError("원본 파일 크기 제한을 초과했어요")
            return content
        except (BotoCoreError, ClientError) as exception:
            raise AppError(
                503,
                "STORAGE_UNAVAILABLE",
                "원본 파일을 읽지 못했어요",
                {"uploadFileId": file.upload_file_id},
            ) from exception

    if settings.storage_base is not None:
        root = Path(settings.storage_base).resolve()
        target = (root / object_key).resolve()
        if root not in target.parents:
            raise CleaningInputError("원본 파일 경로가 올바르지 않아요")
        try:
            if target.stat().st_size > MAX_SOURCE_BYTES:
                raise CleaningInputError("원본 파일 크기 제한을 초과했어요")
            with target.open("rb") as source:
                content = source.read(MAX_SOURCE_BYTES + 1)
            if len(content) > MAX_SOURCE_BYTES:
                raise CleaningInputError("원본 파일 크기 제한을 초과했어요")
            return content
        except OSError as exception:
            raise AppError(
                503,
                "STORAGE_UNAVAILABLE",
                "원본 파일을 읽지 못했어요",
                {"uploadFileId": file.upload_file_id},
            ) from exception

    raise AppError(
        501,
        "STORAGE_NOT_CONFIGURED",
        "원본 파일 저장 위치가 설정되지 않았어요",
        {"uploadFileId": file.upload_file_id},
    )
