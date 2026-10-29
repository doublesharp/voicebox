"""Bounded, temporary quality checks. Speech recognition is explicit and cached-only."""

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import soundfile as sf
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import config
from ..services.sample_quality import analyze_reference, decode_reference

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".webm", ".opus", ".mp4"}


def get_whisper_model():
    # Keep model imports lazy: ordinary waveform analysis must never load ML.
    from ..services.transcribe import get_whisper_model as get_model

    return get_model()


@asynccontextmanager
async def reference_upload(file):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        await file.close()
        raise HTTPException(422, "Choose a WAV, MP3, FLAC, Ogg, M4A, or browser audio recording.")
    managed = config.get_data_dir() / "tmp" / "sample-quality"
    managed.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="check-", dir=managed) as directory:
            path = Path(directory) / ("reference" + suffix)
            with path.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    if output.tell() + len(chunk) > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Reference audio must be no larger than 25 MB.")
                    output.write(chunk)
            try:
                audio, rate = await asyncio.to_thread(decode_reference, path)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            yield audio, rate, Path(directory)
    finally:
        await file.close()


def make_sample_quality_router():
    router = APIRouter(prefix="/sample-quality")

    @router.post("/analyze")
    async def analyze(
        file: UploadFile = File(...), transcript: str = Form("", max_length=10000),
        recognized_text: str | None = Form(None, max_length=10000),
    ):
        async with reference_upload(file) as (audio, rate, _):
            try:
                return await asyncio.to_thread(analyze_reference, audio, rate, transcript, recognized_text)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.post("/transcribe")
    async def transcribe(file: UploadFile = File(...), language: str | None = Form(None, max_length=10)):
        if language is not None:
            from pydantic import ValidationError

            from ..models import TranscriptionRequest

            try:
                TranscriptionRequest(language=language)
            except ValidationError as exc:
                await file.close()
                raise HTTPException(422, "Choose a supported transcription language.") from exc
        model = get_whisper_model()
        size = model.model_size
        if not model.is_loaded() and not model._is_model_cached(size):
            await file.close()
            raise HTTPException(409, "Transcript check needs a downloaded Whisper model. Download it in Server → Models, then try again.")
        async with reference_upload(file) as (audio, rate, directory):
            path = directory / "transcription.wav"
            await asyncio.to_thread(sf.write, str(path), audio.mean(axis=1), rate, format="WAV")
            try:
                text = await model.transcribe(str(path), language, size)
            except Exception as exc:
                raise HTTPException(503, "Transcript check failed. Try again or compare by listening.") from exc
            if not isinstance(text, str):
                raise HTTPException(503, "The transcription model returned invalid text. Compare by listening.")
            return {"text": text, "duration": len(audio) / rate, "model": size}

    return router
