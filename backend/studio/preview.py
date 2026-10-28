"""Bounded, project-rate preview ranges of studio-owned source audio."""

import io
import soundfile as sf
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from .rendering import _source_range
from .storage import NotFound
from .timing import resampled_frames


def make_preview_router(store):
    router = APIRouter()

    @router.get("/assets/{asset_id}/preview")
    def preview(asset_id: str, start_sample: int = Query(ge=0), frames: int = Query(gt=0, le=240000)):
        try:
            path = store.asset_path(asset_id)
            info = sf.info(str(path))
            length = resampled_frames(info.frames, info.samplerate, 48000)
            if start_sample + frames > length:
                raise ValueError("Preview range exceeds source")
            data = _source_range(path, info, 48000, start_sample, start_sample + frames)
            output = io.BytesIO()
            sf.write(output, data, 48000, format="WAV", subtype="FLOAT")
            return Response(output.getvalue(), media_type="audio/wav")
        except NotFound as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    return router
