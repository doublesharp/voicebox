"""Bound reads from uploaded files before passing them to processors."""
from typing import BinaryIO

from fastapi import HTTPException, UploadFile


async def copy_bounded_upload(
    file: UploadFile,
    destination: BinaryIO,
    limit: int,
    *,
    chunk_size: int,
    status: int,
    detail: str,
) -> None:
    """Copy without reading unbounded input or writing bytes past the limit."""
    total = 0
    while chunk := await file.read(min(chunk_size, limit - total + 1)):
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=status, detail=detail)
        destination.write(chunk)
