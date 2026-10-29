"""Owned export files are removed even if a response send is cancelled."""

from pathlib import Path

from fastapi.responses import FileResponse

from .async_work import run_blocking


class TemporaryFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        finally:
            await run_blocking(Path(self.path).unlink, missing_ok=True)
