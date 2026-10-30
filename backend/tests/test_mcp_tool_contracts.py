"""Agent tool contracts at the registered FastMCP boundary."""
import base64
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from backend.mcp_server import tools


@pytest.fixture
def mcp():
    server = FastMCP('test')
    tools.register_tools(server)
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize('args', [{}, {'audio_base64': 'YQ==', 'audio_path': '/file'}, {'audio_base64': '%%%'}])
async def test_transcribe_rejects_missing_ambiguous_or_invalid_audio(mcp, args):
    with pytest.raises(Exception, match='exactly one|Invalid audio_base64'):
        await mcp.call_tool('voicebox_transcribe', args)


@pytest.mark.asyncio
async def test_remote_file_path_is_rejected_before_file_access(mcp, monkeypatch, tmp_path):
    audio = tmp_path / 'voice.wav'
    audio.write_bytes(b'private')
    monkeypatch.setattr(tools, 'request_is_loopback', lambda: False)
    with pytest.raises(Exception, match='only available to loopback'):
        await mcp.call_tool('voicebox_transcribe', {'audio_path': str(audio)})


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['relative.wav', '/this/file/does/not/exist.wav'])
async def test_loopback_file_requires_existing_absolute_path(mcp, monkeypatch, path):
    monkeypatch.setattr(tools, 'request_is_loopback', lambda: True)
    with pytest.raises(Exception, match='must be absolute|File not found'):
        await mcp.call_tool('voicebox_transcribe', {'audio_path': path})


@pytest.mark.asyncio
async def test_loopback_path_forwards_options_without_copying(mcp, monkeypatch, tmp_path):
    audio = tmp_path / 'voice.wav'
    audio.write_bytes(b'local audio')
    monkeypatch.setattr(tools, 'request_is_loopback', lambda: True)
    seen = []
    async def transcribe(path, language, model):
        seen.append((path, path.read_bytes(), language, model))
        return {'text': 'hello', 'duration': 1, 'language': language, 'model': model}
    monkeypatch.setattr(tools, '_transcribe_file', transcribe)
    await mcp.call_tool('voicebox_transcribe', {'audio_path': str(audio), 'language': 'en', 'model': 'base'})
    assert seen == [(audio, b'local audio', 'en', 'base')]
    assert audio.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_base64_temp_file_is_removed_on_success_and_failure(mcp, monkeypatch, fail):
    seen = []
    async def transcribe(path, language, model):
        seen.append(path)
        assert path.read_bytes() == b'audio bytes'
        if fail:
            raise RuntimeError('transcription failed')
        return {'text': 'hello'}
    monkeypatch.setattr(tools, '_transcribe_file', transcribe)
    args = {'audio_base64': base64.b64encode(b'audio bytes').decode()}
    if fail:
        with pytest.raises(Exception, match='transcription failed'):
            await mcp.call_tool('voicebox_transcribe', args)
    else:
        await mcp.call_tool('voicebox_transcribe', args)
    assert len(seen) == 1 and not seen[0].exists()


@pytest.mark.asyncio
async def test_oversized_base64_is_rejected_before_decoding(mcp, monkeypatch):
    monkeypatch.setattr(tools, 'MAX_TRANSCRIBE_BYTES', 3)
    def should_not_decode(*_args, **_kwargs):
        pytest.fail('oversized base64 was decoded into memory')
    monkeypatch.setattr(tools.b64, 'b64decode', should_not_decode)
    with pytest.raises(Exception, match='exceeds'):
        await mcp.call_tool('voicebox_transcribe', {'audio_base64': 'YWFhYWFh'})


@pytest.mark.asyncio
async def test_small_limit_rejects_decoded_audio_even_when_encoded_length_matches(mcp, monkeypatch):
    monkeypatch.setattr(tools, 'MAX_TRANSCRIBE_BYTES', 1)
    with pytest.raises(Exception, match='exceeds'):
        await mcp.call_tool('voicebox_transcribe', {'audio_base64': base64.b64encode(b'ab').decode()})


@pytest.mark.asyncio
async def test_oversized_local_audio_is_rejected_without_transcribing(mcp, monkeypatch, tmp_path):
    audio = tmp_path / 'voice.wav'
    audio.write_bytes(b'1234')
    monkeypatch.setattr(tools, 'request_is_loopback', lambda: True)
    monkeypatch.setattr(tools, 'MAX_TRANSCRIBE_BYTES', 3)
    with pytest.raises(Exception, match='exceeds'):
        await mcp.call_tool('voicebox_transcribe', {'audio_path': str(audio)})


@pytest.mark.asyncio
@pytest.mark.parametrize('limit,offset', [(0, 0), (201, 0), (20, -1)])
async def test_capture_pagination_rejects_out_of_range(mcp, limit, offset):
    with pytest.raises(Exception, match='limit|offset'):
        await mcp.call_tool('voicebox_list_captures', {'limit': limit, 'offset': offset})


@pytest.mark.asyncio
async def test_profile_listing_closes_database_and_filters_personality(mcp, monkeypatch):
    closed = []
    db = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(tools, 'get_db', lambda: iter([db]))
    async def list_profiles(_db):
        assert _db is db
        return [SimpleNamespace(id='p', name='Voice', voice_type='cloned', language='en', personality='secret prompt')]
    monkeypatch.setattr(tools.profiles_service, 'list_profiles', list_profiles)
    result = await mcp.call_tool('voicebox_list_profiles', {})
    assert result.structured_content == {'profiles': [{'id': 'p', 'name': 'Voice', 'voice_type': 'cloned', 'language': 'en', 'has_personality': True}]}
    assert closed == [True]


@pytest.mark.asyncio
async def test_base64_audio_with_empty_optional_path_is_transcribed(mcp, monkeypatch):
    received = []
    async def transcribe(path, language, model):
        received.append(path.read_bytes())
        return {'text': 'hello'}
    monkeypatch.setattr(tools, '_transcribe_file', transcribe)
    await mcp.call_tool('voicebox_transcribe', {'audio_base64': 'YXVkaW8=', 'audio_path': ''})
    assert received == [b'audio']


@pytest.mark.asyncio
async def test_temporary_audio_is_removed_when_write_fails(mcp, monkeypatch):
    original = tools.tempfile.NamedTemporaryFile
    created = []
    class FailingFile:
        def __init__(self, *args, **kwargs):
            self.inner = original(*args, **kwargs)
            self.name = self.inner.name
            created.append(self.name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.inner.close()
        def write(self, _data):
            raise OSError('disk full')
    monkeypatch.setattr(tools.tempfile, 'NamedTemporaryFile', FailingFile)
    with pytest.raises(Exception, match='disk full'):
        await mcp.call_tool('voicebox_transcribe', {'audio_base64': 'YXVkaW8='})
    from pathlib import Path
    assert len(created) == 1 and not Path(created[0]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('loaded,cached,selected,current,expected', [
    (True, False, 'base', 'base', True),
    (False, True, 'small', 'base', True),
    (False, False, 'small', 'base', False),
    (True, False, 'small', 'base', False),
])
async def test_transcribe_helper_requires_selected_model_to_be_available(monkeypatch, tmp_path, loaded, cached, selected, current, expected):
    from backend.services import transcribe as service
    from backend.utils import audio
    calls = []
    class Whisper:
        model_size = current
        def is_loaded(self):
            return loaded
        def _is_model_cached(self, model):
            calls.append(('cache', model))
            return cached
        async def transcribe(self, path, language, model):
            calls.append(('transcribe', path, language, model))
            return 'words'
    monkeypatch.setattr(service, 'get_whisper_model', lambda: Whisper())
    monkeypatch.setattr(audio, 'load_audio', lambda _path: ([0.0] * 8000, 16000))
    path = tmp_path / 'clip.wav'
    if expected:
        result = await tools._transcribe_file(path, 'en', selected)
        assert result == {'text': 'words', 'duration': 0.5, 'language': 'en', 'model': selected}
        assert ('transcribe', str(path), 'en', selected) in calls
    else:
        with pytest.raises(ValueError, match='not yet downloaded'):
            await tools._transcribe_file(path, 'en', selected)
        assert not any(call[0] == 'transcribe' for call in calls)


@pytest.mark.asyncio
async def test_transcribe_helper_rejects_unknown_model_before_reading_audio(monkeypatch, tmp_path):
    from backend.services import transcribe as service
    from backend.utils import audio
    monkeypatch.setattr(service, 'get_whisper_model', lambda: SimpleNamespace(model_size='base'))
    monkeypatch.setattr(audio, 'load_audio', lambda *_args: pytest.fail('invalid model read audio'))
    with pytest.raises(ValueError, match='Invalid STT model'):
        await tools._transcribe_file(tmp_path / 'clip.wav', None, 'bogus')
