"""Exercise codec capability checks through the speech API and encoder."""
import httpx
import numpy as np
import pytest
from fastapi import FastAPI

import backend.mcp_server.resolve as resolve_mod
from backend.database import get_db
from backend.routes import openai_compat


@pytest.mark.asyncio
@pytest.mark.parametrize('response_format', ['mp3', 'opus'])
async def test_unavailable_codec_is_rejected_before_synthesis(response_format, monkeypatch):
    container, _, _ = openai_compat._SOUNDFILE_FORMATS[response_format]
    monkeypatch.setattr(openai_compat.sf, 'available_formats', lambda: {container: 'available'})
    monkeypatch.setattr(openai_compat.sf, 'check_format', lambda fmt, sub: False)
    monkeypatch.setattr(resolve_mod, 'resolve_profile', lambda *_: None)
    app = FastAPI()
    app.include_router(openai_compat.router)
    app.dependency_overrides[get_db] = lambda: object()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/audio/speech', json={
            'model': 'tts-1', 'voice': 'Morgan', 'input': 'Hello', 'response_format': response_format,
        })
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'unsupported_format'
    assert response.json()['error']['param'] == 'response_format'


@pytest.mark.parametrize('response_format', ['mp3', 'opus'])
def test_encoder_rejects_missing_codec(response_format, monkeypatch):
    container, _, _ = openai_compat._SOUNDFILE_FORMATS[response_format]
    monkeypatch.setattr(openai_compat.sf, 'available_formats', lambda: {container: 'available'})
    monkeypatch.setattr(openai_compat.sf, 'check_format', lambda fmt, sub: False)
    with pytest.raises(openai_compat.OpenAIError) as error:
        openai_compat.encode_audio(np.zeros(100, dtype=np.float32), 24000, response_format)
    assert error.value.code == 'unsupported_format'


@pytest.mark.parametrize('response_format', ['wav', 'flac', 'mp3', 'opus'])
def test_available_formats_require_both_container_and_codec(response_format, monkeypatch):
    container, subtype, _ = openai_compat._SOUNDFILE_FORMATS[response_format]
    monkeypatch.setattr(openai_compat.sf, 'available_formats', lambda: {container: 'available'})
    monkeypatch.setattr(openai_compat.sf, 'check_format', lambda fmt, sub: (fmt, sub) == (container, subtype))
    monkeypatch.setattr(openai_compat.shutil, 'which', lambda _: None)
    assert response_format in openai_compat._available_formats()
    assert 'pcm' in openai_compat._available_formats()


@pytest.mark.parametrize('response_format', ['wav', 'flac'])
def test_real_encoder_round_trips_available_lossless_formats(response_format):
    import io
    import soundfile as sf
    container, subtype, media_type = openai_compat._SOUNDFILE_FORMATS[response_format]
    if container not in sf.available_formats() or not sf.check_format(container, subtype):
        pytest.skip(f'{response_format} is unavailable in this libsndfile build')
    audio = np.array([0.0, 0.25, -0.25, 0.0], dtype=np.float32)
    content, actual_media_type = openai_compat.encode_audio(audio, 24000, response_format)
    decoded, sample_rate = sf.read(io.BytesIO(content))
    assert actual_media_type == media_type
    assert sample_rate == 24000
    np.testing.assert_allclose(decoded, audio, atol=1 / 32767)


def test_pcm_clips_and_returns_little_endian_samples():
    audio = np.array([-2, -0.5, 0, 0.5, 2], dtype=np.float32)
    data, media_type = openai_compat.encode_audio(audio, 24000, 'pcm')
    assert media_type == 'audio/pcm'
    assert np.frombuffer(data, dtype='<i2').tolist() == [-32767, -16383, 0, 16383, 32767]


@pytest.mark.parametrize('failure', ['timeout', 'nonzero'])
def test_aac_encoder_reports_ffmpeg_failures(failure, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    monkeypatch.setattr(openai_compat.shutil, 'which', lambda _: '/usr/bin/ffmpeg')
    def run(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('ffmpeg', 120)
        return SimpleNamespace(returncode=1, stderr=b'encoder unavailable')
    monkeypatch.setattr(openai_compat.subprocess, 'run', run)
    with pytest.raises(openai_compat.OpenAIError) as error:
        openai_compat.encode_audio(np.zeros(100, dtype=np.float32), 24000, 'aac')
    assert error.value.status_code == 500
    assert error.value.error_type == 'server_error'
    assert ('timed out' if failure == 'timeout' else 'encoder unavailable') in error.value.message
