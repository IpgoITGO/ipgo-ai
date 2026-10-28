"""원본 엑셀 읽기. 저장 위치(서버 디스크 / S3 등)가 정해지기 전까지는 예외로 처리한다."""
from app.config import get_settings
from app.errors import AppError
from schemas.engine import SourceFile


def read_source_file(file: SourceFile) -> bytes:
    settings = get_settings()
    if settings.storage_base is None:
        raise AppError(
            501,
            "STORAGE_NOT_CONFIGURED",
            "원본 파일 저장 위치가 아직 정해지지 않았어요",
            {"uploadFileId": file.upload_file_id, "storageKey": file.storage_key},
        )
    # TODO(합의): 저장 방식이 정해지면 구현 (docs/decisions/0001)
    raise NotImplementedError("storage reader")
