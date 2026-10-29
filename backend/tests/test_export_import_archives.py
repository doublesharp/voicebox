"""Archive integrity checks for saved voice work."""

import io
import json
import math
import struct
import wave
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database import Base, Generation, GenerationVersion, ProfileSample, VoiceProfile
from backend.services import export_import


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def archive(manifest, samples=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        if samples is not None:
            z.writestr("samples.json", json.dumps(samples))
    return out.getvalue()


def reference_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / 16000)))
                                 for i in range(48000)))
    return output.getvalue()


@pytest.mark.asyncio
async def test_failed_profile_sample_import_removes_partial_profile_and_files(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="existing", name="Existing"))
    db.commit()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("manifest.json", json.dumps({"version": "1.0", "profile": {"name": "Imported"}}))
        z.writestr("samples.json", json.dumps({"valid.wav": "first", "broken.wav": "second"}))
        z.writestr("samples/valid.wav", reference_wav())
        z.writestr("samples/broken.wav", b"not audio")
    with pytest.raises(ValueError, match="Invalid reference audio"):
        await export_import.import_profile_from_zip(data.getvalue(), db)
    assert [p.name for p in db.query(VoiceProfile).all()] == ["Existing"]
    assert db.query(ProfileSample).count() == 0
    assert list(config.get_profiles_dir().iterdir()) == []


@pytest.mark.asyncio
async def test_profile_import_commits_all_samples(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="existing", name="Voice"))
    db.commit()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("manifest.json", json.dumps({"version": "1.0", "profile": {"name": "Voice"}}))
        z.writestr("samples.json", json.dumps({"a.wav": "first", "b.wav": "second"}))
        z.writestr("samples/a.wav", reference_wav())
        z.writestr("samples/b.wav", reference_wav())
    result = await export_import.import_profile_from_zip(data.getvalue(), db)
    assert result.name == "Voice (1)"
    samples = db.query(ProfileSample).filter_by(profile_id=result.id).all()
    assert {sample.reference_text for sample in samples} == {"first", "second"}
    assert all(config.resolve_storage_path(sample.audio_path).is_file() for sample in samples)


@pytest.mark.asyncio
async def test_profile_import_cleans_up_if_commit_fails(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("manifest.json", json.dumps({"version": "1.0", "profile": {"name": "Voice"}}))
        z.writestr("samples.json", json.dumps({"a.wav": "first"}))
        z.writestr("samples/a.wav", reference_wav())
    def fail_commit():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(ValueError, match="database unavailable"):
        await export_import.import_profile_from_zip(data.getvalue(), db)
    assert db.query(VoiceProfile).count() == 0
    assert db.query(ProfileSample).count() == 0
    assert list(config.get_profiles_dir().iterdir()) == []


@pytest.mark.asyncio
async def test_invalid_profile_archive_does_not_create_a_profile(db, monkeypatch):
    async def unexpected_create(*args):
        pytest.fail("a profile must not be created before archive validation")

    monkeypatch.setattr(export_import, "create_profile", unexpected_create)
    data = archive({"version": "1.0", "profile": {"name": "Voice"}}, {"missing.wav": "words"})
    with pytest.raises(ValueError, match="Sample file not found"):
        await export_import.import_profile_from_zip(data, db)
    assert db.query(VoiceProfile).count() == 0


@pytest.mark.parametrize("path", [None, "missing.wav"])
def test_generation_export_rejects_missing_version_audio(db, monkeypatch, tmp_path, path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.add(Generation(id="g", profile_id="p", text="Hello", language="en", created_at=datetime.now(timezone.utc)))
    db.add(GenerationVersion(id="v", generation_id="g", label="Take", audio_path=path or "", is_default=True))
    db.commit()
    with pytest.raises(ValueError, match="Audio file not found"):
        export_import.export_generation_to_zip("g", db)


def test_generation_export_contains_all_version_audio_and_metadata(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    (tmp_path / "one.wav").write_bytes(b"first")
    (tmp_path / "two.wav").write_bytes(b"second")
    db.add(VoiceProfile(id="p", name="Voice"))
    db.add(Generation(id="g", profile_id="p", text="Hello", language="en", created_at=datetime.now(timezone.utc)))
    db.add_all([
        GenerationVersion(id="v1", generation_id="g", label="Original", audio_path="one.wav", is_default=True),
        GenerationVersion(id="v2", generation_id="g", label="Edited", audio_path="two.wav", effects_chain='[{"type":"gain"}]'),
    ])
    db.commit()
    with zipfile.ZipFile(io.BytesIO(export_import.export_generation_to_zip("g", db))) as z:
        assert z.read("audio/one.wav") == b"first"
        assert z.read("audio/two.wav") == b"second"
        manifest = json.loads(z.read("manifest.json"))
        assert {v["id"] for v in manifest["versions"]} == {"v1", "v2"}
        assert next(v for v in manifest["versions"] if v["id"] == "v2")["effects_chain"] == [{"type": "gain"}]


@pytest.mark.asyncio
async def test_generation_import_uses_selected_default_take(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.commit()
    manifest = {
        "version": "1.0",
        "profile": {"name": "Voice"},
        "generation": {"text": "Hello", "language": "en", "duration": 1.0},
        "versions": [
            {"filename": "one.wav", "is_default": False},
            {"filename": "two.wav", "is_default": True},
        ],
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("audio/one.wav", b"original")
        z.writestr("audio/two.wav", b"chosen take")
    result = await export_import.import_generation_from_zip(out.getvalue(), db)
    generation = db.query(Generation).filter_by(id=result["id"]).one()
    assert config.resolve_storage_path(generation.audio_path).read_bytes() == b"chosen take"


@pytest.mark.asyncio
async def test_generation_import_rejects_missing_default_take(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.commit()
    manifest = {"version": "1.0", "generation": {"text": "Hi", "language": "en", "duration": 1},
                "versions": [{"filename": "missing.wav", "is_default": True}]}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("audio/other.wav", b"other")
    with pytest.raises(ValueError, match="Default version audio not found"):
        await export_import.import_generation_from_zip(out.getvalue(), db)
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_generation_import_removes_audio_when_database_commit_fails(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.commit()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("manifest.json", json.dumps({"version": "1.0", "profile": {"name": "Voice"},
                                               "generation": {"text": "Hi", "language": "en", "duration": 1}}))
        z.writestr("audio/legacy.wav", b"audio")
    def fail_commit():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(ValueError, match="database unavailable"):
        await export_import.import_generation_from_zip(data.getvalue(), db)
    assert db.query(Generation).count() == 0
    assert list(config.get_generations_dir().iterdir()) == []


@pytest.mark.parametrize("path", [None, "missing.wav"])
def test_generation_export_rejects_missing_legacy_audio(db, monkeypatch, tmp_path, path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.add(Generation(id="g", profile_id="p", text="Hi", audio_path=path,
                      created_at=datetime.now(timezone.utc)))
    db.commit()
    with pytest.raises(ValueError, match="Audio file not found"):
        export_import.export_generation_to_zip("g", db)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['profile', 'generation'])
async def test_compressed_expanded_archive_limit_is_checked_before_mutation(kind, db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    monkeypatch.setattr(export_import, 'MAX_IMPORT_EXPANDED_BYTES', 256, raising=False)
    db.add(VoiceProfile(id='existing', name='Voice'))
    db.commit()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        if kind == 'profile':
            z.writestr('manifest.json', json.dumps({'version': '1.0', 'profile': {'name': 'New'}}))
            z.writestr('samples.json', json.dumps({'sample.wav': 'words'}))
            z.writestr('samples/sample.wav', b'0' * 4096)
        else:
            z.writestr('manifest.json', json.dumps({'version': '1.0', 'generation': {'text': 'Hello', 'language': 'en', 'duration': 1}}))
            z.writestr('audio/sample.wav', b'0' * 4096)
    assert len(output.getvalue()) < 1024
    with pytest.raises(ValueError, match='archive exceeds limits'):
        if kind == 'profile':
            await export_import.import_profile_from_zip(output.getvalue(), db)
        else:
            await export_import.import_generation_from_zip(output.getvalue(), db)
    assert db.query(VoiceProfile).count() == 1
    assert db.query(Generation).count() == 0
    assert db.query(ProfileSample).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['profile', 'generation'])
async def test_duplicate_manifest_is_rejected_before_mutation(kind, db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='existing', name='Voice'))
    db.commit()
    manifest = {'version': '1.0', 'profile': {'name': 'New'}} if kind == 'profile' else {
        'version': '1.0', 'generation': {'text': 'Hi', 'language': 'en', 'duration': 1}}
    output = io.BytesIO()
    with pytest.warns(UserWarning, match='Duplicate name'):
        with zipfile.ZipFile(output, 'w') as z:
            z.writestr('manifest.json', json.dumps(manifest))
            z.writestr('manifest.json', json.dumps(manifest))
            if kind == 'profile': z.writestr('samples.json', '{}')
            else: z.writestr('audio/test.wav', b'audio')
    with pytest.raises(ValueError, match='duplicate archive entry'):
        if kind == 'profile':
            await export_import.import_profile_from_zip(output.getvalue(), db)
        else:
            await export_import.import_generation_from_zip(output.getvalue(), db)
    assert db.query(VoiceProfile).count() == 1
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['profile', 'generation'])
async def test_import_streams_audio_instead_of_reading_full_member(kind, db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='existing', name='Voice'))
    db.commit()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        if kind == 'profile':
            z.writestr('manifest.json', json.dumps({'version': '1.0', 'profile': {'name': 'New'}}))
            z.writestr('samples.json', json.dumps({'sample.wav': 'words'}))
            z.writestr('samples/sample.wav', reference_wav())
        else:
            z.writestr('manifest.json', json.dumps({'version': '1.0', 'generation': {'text': 'Hi', 'language': 'en', 'duration': 1}}))
            z.writestr('audio/sample.wav', b'audio data')
    original_read = zipfile.ZipFile.read
    def no_whole_audio(self, name, *args, **kwargs):
        assert not str(name).startswith(('samples/', 'audio/')), 'audio must be streamed'
        return original_read(self, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'read', no_whole_audio)
    if kind == 'profile':
        result = await export_import.import_profile_from_zip(output.getvalue(), db)
        assert result.name == 'New'
        assert db.query(ProfileSample).count() == 1
    else:
        result = await export_import.import_generation_from_zip(output.getvalue(), db)
        generation = db.query(Generation).filter_by(id=result['id']).one()
        assert config.resolve_storage_path(generation.audio_path).read_bytes() == b'audio data'


@pytest.mark.asyncio
async def test_failed_optional_avatar_extraction_removes_its_temporary_file(db, monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    temp = tmp_path / 'uploads'
    temp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temp))
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('manifest.json', json.dumps({'version': '1.0', 'profile': {'name': 'New'}}))
        z.writestr('samples.json', '{}')
        z.writestr('avatar.png', b'image')
    original_open = zipfile.ZipFile.open
    def broken_avatar(self, name, *args, **kwargs):
        if name == 'avatar.png': raise OSError('broken avatar')
        return original_open(self, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'open', broken_avatar)
    result = await export_import.import_profile_from_zip(output.getvalue(), db)
    assert result.name == 'New'
    assert list(temp.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', ['manifest.json', 'samples.json'])
async def test_oversized_profile_metadata_is_rejected_before_profile_creation(metadata, db, monkeypatch):
    monkeypatch.setattr(export_import, 'MAX_IMPORT_METADATA_BYTES', 32)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps({'version': '1.0', 'profile': {'name': 'Voice'}}))
        z.writestr('samples.json', json.dumps({'x' * 80: 'words'}) if metadata == 'samples.json' else '{}')
    if metadata == 'samples.json':
        # Keep the valid manifest below this bound while exceeding it for samples.
        monkeypatch.setattr(export_import, 'MAX_IMPORT_METADATA_BYTES', 80)
    with pytest.raises(ValueError, match=f'archive metadata exceeds limits: {metadata}'):
        await export_import.import_profile_from_zip(output.getvalue(), db)
    assert db.query(VoiceProfile).count() == 0


@pytest.mark.asyncio
async def test_archive_entry_limit_accepts_boundary_and_rejects_extra_entry(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    monkeypatch.setattr(export_import, 'MAX_IMPORT_ENTRIES', 2)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('manifest.json', json.dumps({'version': '1.0', 'profile': {'name': 'Voice'}}))
        z.writestr('samples.json', '{}')
    result = await export_import.import_profile_from_zip(output.getvalue(), db)
    assert result.name == 'Voice'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('manifest.json', json.dumps({'version': '1.0', 'profile': {'name': 'Extra'}}))
        z.writestr('samples.json', '{}')
        z.writestr('extra.txt', 'extra')
    with pytest.raises(ValueError, match='archive exceeds limits'):
        await export_import.import_profile_from_zip(output.getvalue(), db)
    assert db.query(VoiceProfile).count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['profile', 'generation'])
async def test_interrupted_audio_extraction_rolls_back_and_removes_temporary_file(kind, db, monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    temp = tmp_path / 'uploads'
    temp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temp))
    db.add(VoiceProfile(id='existing', name='Voice'))
    db.commit()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        if kind == 'profile':
            z.writestr('manifest.json', json.dumps({'version': '1.0', 'profile': {'name': 'New'}}))
            z.writestr('samples.json', json.dumps({'sample.wav': 'words'}))
            z.writestr('samples/sample.wav', b'audio')
            target = 'samples/sample.wav'
        else:
            z.writestr('manifest.json', json.dumps({'version': '1.0', 'generation': {'text': 'Hi', 'language': 'en', 'duration': 1}}))
            z.writestr('audio/sample.wav', b'audio')
            target = 'audio/sample.wav'
    original_open = zipfile.ZipFile.open
    class Interrupted:
        calls = 0
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size):
            assert 0 < size <= export_import.IMPORT_COPY_CHUNK_BYTES
            self.calls += 1
            if self.calls == 1: return b'partial'
            raise OSError('archive read interrupted')
    def broken_audio(self, name, *args, **kwargs):
        if name == target: return Interrupted()
        return original_open(self, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'open', broken_audio)
    with pytest.raises(ValueError, match='archive read interrupted'):
        if kind == 'profile':
            await export_import.import_profile_from_zip(output.getvalue(), db)
        else:
            await export_import.import_generation_from_zip(output.getvalue(), db)
    assert db.query(VoiceProfile).count() == 1
    assert db.query(Generation).count() == 0
    assert db.query(ProfileSample).count() == 0
    assert list(temp.iterdir()) == []


@pytest.mark.asyncio
async def test_generation_archive_roundtrip_preserves_all_takes_and_source_relationships(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    (tmp_path / 'one.wav').write_bytes(b'clean audio')
    (tmp_path / 'two.wav').write_bytes(b'processed audio')
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.add(Generation(id='original', profile_id='voice', text='Hello', language='en', duration=2,
                      audio_path='two.wav', engine='chatterbox', model_size='small', synthesis_config='{"speed": 0.8}',
                      source='rest', is_favorited=True, created_at=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)))
    db.add_all([
        GenerationVersion(id='clean', generation_id='original', label='Clean', audio_path='one.wav', is_default=False,
                          created_at=datetime(2023, 1, 2, tzinfo=timezone.utc)),
        GenerationVersion(id='processed', generation_id='original', label='Processed', audio_path='two.wav',
                          effects_chain='[{"type":"gain"}]', source_version_id='clean', is_default=True,
                          created_at=datetime(2023, 1, 3, tzinfo=timezone.utc)),
    ])
    db.commit()
    archive_bytes = export_import.export_generation_to_zip('original', db)
    result = await export_import.import_generation_from_zip(archive_bytes, db)
    imported = db.query(Generation).filter_by(id=result['id']).one()
    versions = db.query(GenerationVersion).filter_by(generation_id=imported.id).all()
    by_label = {v.label: v for v in versions}
    assert set(by_label) == {'Clean', 'Processed'}
    assert by_label['Clean'].id != 'clean'
    assert by_label['Processed'].source_version_id == by_label['Clean'].id
    assert json.loads(by_label['Processed'].effects_chain) == [{'type': 'gain'}]
    assert by_label['Processed'].is_default is True
    assert by_label['Clean'].is_default is False
    assert by_label['Clean'].created_at.replace(tzinfo=timezone.utc) == datetime(2023, 1, 2, tzinfo=timezone.utc)
    assert by_label['Processed'].created_at.replace(tzinfo=timezone.utc) == datetime(2023, 1, 3, tzinfo=timezone.utc)
    assert imported.audio_path == by_label['Processed'].audio_path
    assert config.resolve_storage_path(by_label['Clean'].audio_path).read_bytes() == b'clean audio'
    assert config.resolve_storage_path(by_label['Processed'].audio_path).read_bytes() == b'processed audio'
    assert (imported.engine, imported.model_size) == ('chatterbox', 'small')
    assert json.loads(imported.synthesis_config) == {'speed': 0.8}
    assert imported.source == 'rest' and imported.is_favorited is True
    assert imported.created_at.replace(tzinfo=timezone.utc) == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_missing_nondefault_take_is_rejected_before_any_write(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('manifest.json', json.dumps({
            'version': '1.0', 'profile': {'name': 'Voice'},
            'generation': {'text': 'Hi', 'language': 'en', 'duration': 1},
            'versions': [
                {'id': 'missing', 'label': 'Missing', 'filename': 'missing.wav', 'is_default': False},
                {'id': 'present', 'label': 'Present', 'filename': 'present.wav', 'is_default': True},
            ],
        }))
        z.writestr('audio/present.wav', b'present')
    with pytest.raises(ValueError, match='version audio not found'):
        await export_import.import_generation_from_zip(output.getvalue(), db)
    assert db.query(Generation).count() == 0
    assert db.query(GenerationVersion).count() == 0
    assert list(config.get_generations_dir().iterdir()) == []


@pytest.mark.asyncio
async def test_generation_export_handles_takes_with_the_same_basename(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    (tmp_path / 'one').mkdir()
    (tmp_path / 'two').mkdir()
    (tmp_path / 'one' / 'take.wav').write_bytes(b'first')
    (tmp_path / 'two' / 'take.wav').write_bytes(b'second')
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.add(Generation(id='original', profile_id='voice', text='Hi', language='en', duration=1,
                      audio_path='one/take.wav', created_at=datetime.now(timezone.utc)))
    db.add_all([
        GenerationVersion(id='one', generation_id='original', label='One', audio_path='one/take.wav', is_default=True),
        GenerationVersion(id='two', generation_id='original', label='Two', audio_path='two/take.wav', is_default=False),
    ])
    db.commit()
    result = await export_import.import_generation_from_zip(export_import.export_generation_to_zip('original', db), db)
    versions = db.query(GenerationVersion).filter_by(generation_id=result['id']).all()
    assert {v.label: config.resolve_storage_path(v.audio_path).read_bytes() for v in versions} == {'One': b'first', 'Two': b'second'}


def generation_take_archive(versions, files=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('manifest.json', json.dumps({
            'version': '1.0', 'profile': {'name': 'Voice'},
            'generation': {'text': 'Hi', 'language': 'en', 'duration': 1},
            'versions': versions,
        }))
        for name, data in (files or {'one.wav': b'one', 'two.wav': b'two'}).items():
            z.writestr('audio/' + name, data)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('versions', [
    [{'id': 'same', 'filename': 'one.wav'}, {'id': 'same', 'filename': 'two.wav'}],
    [{'id': 'one', 'filename': 'one.wav', 'is_default': True}, {'id': 'two', 'filename': 'two.wav', 'is_default': True}],
    [{'id': 'one', 'filename': 'one.wav', 'source_version_id': 'missing'}],
    [{'id': 'one', 'filename': 'one.wav', 'source_version_id': 'two'}, {'id': 'two', 'filename': 'two.wav', 'source_version_id': 'one'}],
    [{'id': 'one', 'filename': 'one.wav', 'effects_chain': 'invalid'}],
    [{'id': 'one', 'filename': 'one.wav', 'is_default': 'false'}],
    [{'id': 'one', 'filename': 'one.wav', 'created_at': 'not a date'}],
])
async def test_invalid_take_metadata_is_rejected_without_writes(versions, db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    with pytest.raises(ValueError):
        await export_import.import_generation_from_zip(generation_take_archive(versions), db)
    assert db.query(Generation).count() == 0
    assert db.query(GenerationVersion).count() == 0
    assert list(config.get_generations_dir().iterdir()) == []


@pytest.mark.asyncio
async def test_forward_source_relationship_and_missing_default_are_restored(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    versions = [
        {'id': 'derived', 'label': 'Derived', 'filename': 'two.wav', 'source_version_id': 'original', 'effects_chain': []},
        {'id': 'original', 'label': 'Original', 'filename': 'one.wav'},
    ]
    result = await export_import.import_generation_from_zip(generation_take_archive(versions), db)
    rows = {v.label: v for v in db.query(GenerationVersion).filter_by(generation_id=result['id']).all()}
    assert rows['Derived'].source_version_id == rows['Original'].id
    assert rows['Derived'].is_default is True
    assert db.get(Generation, result['id']).audio_path == rows['Derived'].audio_path


@pytest.mark.asyncio
async def test_import_commit_failure_removes_every_created_take(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    existing = config.get_generations_dir() / 'keep.wav'
    existing.write_bytes(b'keep')
    versions = [{'id': 'one', 'filename': 'one.wav'}, {'id': 'two', 'filename': 'two.wav'}]
    def fail_commit(): raise RuntimeError('commit failed')
    monkeypatch.setattr(db, 'commit', fail_commit)
    with pytest.raises(ValueError, match='commit failed'):
        await export_import.import_generation_from_zip(generation_take_archive(versions), db)
    assert db.query(Generation).count() == 0
    assert db.query(GenerationVersion).count() == 0
    assert list(config.get_generations_dir().iterdir()) == [existing]
    assert existing.read_bytes() == b'keep'


@pytest.mark.asyncio
async def test_import_failure_on_second_take_removes_first_take(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    existing = config.get_generations_dir() / 'keep.wav'
    existing.write_bytes(b'keep')
    versions = [{'id': 'one', 'filename': 'one.wav'}, {'id': 'two', 'filename': 'two.wav'}]
    data = generation_take_archive(versions)
    original_open = zipfile.ZipFile.open
    def fail_second(self, name, *args, **kwargs):
        if name == 'audio/two.wav': raise OSError('second take failed')
        return original_open(self, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'open', fail_second)
    with pytest.raises(ValueError, match='second take failed'):
        await export_import.import_generation_from_zip(data, db)
    assert db.query(Generation).count() == 0
    assert db.query(GenerationVersion).count() == 0
    assert list(config.get_generations_dir().iterdir()) == [existing]
    assert existing.read_bytes() == b'keep'


@pytest.mark.asyncio
async def test_legacy_single_audio_import_creates_one_default_take(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    result = await export_import.import_generation_from_zip(generation_take_archive([]), db)
    generation = db.get(Generation, result['id'])
    version = db.query(GenerationVersion).filter_by(generation_id=result['id']).one()
    assert version.label == 'original'
    assert version.is_default is True
    assert version.audio_path == generation.audio_path
    assert config.resolve_storage_path(version.audio_path).read_bytes() == b'one'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('created_at', 'not a date'),
    ('created_at', 42),
    ('source', 42),
    ('synthesis_config', {'unexpected': 'object'}),
    ('is_favorited', 'yes'),
])
async def test_invalid_generation_metadata_is_rejected_before_writing(field, value, db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    data = io.BytesIO()
    generation = {'text': 'Hi', 'language': 'en', 'duration': 1, field: value}
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('manifest.json', json.dumps({'version': '1.0', 'generation': generation}))
        z.writestr('audio/one.wav', b'one')
    with pytest.raises(ValueError):
        await export_import.import_generation_from_zip(data.getvalue(), db)
    assert db.query(Generation).count() == 0
    assert db.query(GenerationVersion).count() == 0
    assert list(config.get_generations_dir().iterdir()) == []


@pytest.mark.asyncio
async def test_committed_generation_import_does_not_depend_on_post_commit_refresh(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    def fail_refresh(*args, **kwargs):
        raise RuntimeError('refresh unavailable after commit')
    monkeypatch.setattr(db, 'refresh', fail_refresh)
    result = await export_import.import_generation_from_zip(generation_take_archive([]), db)
    assert db.query(Generation).filter_by(id=result['id']).one().text == 'Hi'
    assert db.query(GenerationVersion).filter_by(generation_id=result['id']).count() == 1


def test_generation_export_uses_the_validated_take_path(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    audio = tmp_path / 'take.wav'
    audio.write_bytes(b'take')
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.add(Generation(id='original', profile_id='voice', text='Hi', language='en', duration=1,
                      audio_path='take.wav', created_at=datetime.now(timezone.utc)))
    db.add(GenerationVersion(id='one', generation_id='original', label='One', audio_path='take.wav', is_default=True))
    db.commit()
    original_resolve = config.resolve_storage_path
    calls = 0
    def intermittent_resolve(path):
        nonlocal calls
        calls += 1
        return original_resolve(path) if calls == 1 else None
    monkeypatch.setattr(config, 'resolve_storage_path', intermittent_resolve)
    archive_bytes = export_import.export_generation_to_zip('original', db)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as z:
        assert z.read('audio/take.wav') == b'take'
    assert calls == 1


@pytest.mark.asyncio
async def test_forward_source_import_succeeds_with_sqlite_foreign_keys_enabled(db, monkeypatch, tmp_path):
    from sqlalchemy import text
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.execute(text('PRAGMA foreign_keys=ON'))
    assert db.execute(text('PRAGMA foreign_keys')).scalar_one() == 1
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.commit()
    versions = [
        {'id': 'derived', 'label': 'Derived', 'filename': 'two.wav', 'source_version_id': 'original'},
        {'id': 'original', 'label': 'Original', 'filename': 'one.wav'},
    ]
    result = await export_import.import_generation_from_zip(generation_take_archive(versions), db)
    rows = {v.label: v for v in db.query(GenerationVersion).filter_by(generation_id=result['id']).all()}
    assert rows['Derived'].source_version_id == rows['Original'].id
    assert db.execute(text('PRAGMA foreign_key_check')).all() == []


@pytest.mark.asyncio
async def test_cloned_profile_archive_roundtrip_preserves_voice_settings_and_samples(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    audio = tmp_path / 'sample.wav'
    audio.write_bytes(reference_wav())
    db.add(VoiceProfile(id='voice', name='Voice', language='en', voice_type='cloned',
                        default_engine='chatterbox', personality='Warm storyteller',
                        effects_chain=json.dumps([{'type': 'gain', 'enabled': True, 'params': {'gain_db': -4}}])))
    db.add(ProfileSample(id='sample', profile_id='voice', audio_path='sample.wav', reference_text='Hello'))
    db.commit()
    result = await export_import.import_profile_from_zip(export_import.export_profile_to_zip('voice', db), db)
    restored = db.get(VoiceProfile, result.id)
    assert result.name == 'Voice (1)'
    assert restored.voice_type == 'cloned'
    assert restored.default_engine == 'chatterbox'
    assert restored.personality == 'Warm storyteller'
    assert json.loads(restored.effects_chain) == [{'type': 'gain', 'enabled': True, 'params': {'gain_db': -4}}]
    assert result.effects_chain[0].type == 'gain'
    assert result.sample_count == 1
    assert db.query(ProfileSample).filter_by(profile_id=result.id).one().reference_text == 'Hello'


@pytest.mark.asyncio
@pytest.mark.parametrize('fields', [
    {'voice_type': 'preset', 'preset_engine': 'kokoro', 'preset_voice_id': 'af_heart', 'default_engine': 'kokoro'},
    {'voice_type': 'designed', 'design_prompt': 'A warm, calm narrator', 'default_engine': 'qwen_voice_design'},
])
async def test_sample_free_preset_and_designed_profiles_can_roundtrip(fields, db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice', language='en', personality='Tell stories', **fields))
    db.commit()
    result = await export_import.import_profile_from_zip(export_import.export_profile_to_zip('voice', db), db)
    restored = db.get(VoiceProfile, result.id)
    assert result.name == 'Voice (1)'
    for key, value in fields.items():
        assert getattr(restored, key) == value
    assert restored.personality == 'Tell stories'
    assert db.query(ProfileSample).filter_by(profile_id=result.id).count() == 0


@pytest.mark.asyncio
async def test_profile_export_preserves_samples_with_identical_basenames(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    (tmp_path / 'one').mkdir()
    (tmp_path / 'two').mkdir()
    (tmp_path / 'one' / 'sample.wav').write_bytes(reference_wav())
    (tmp_path / 'two' / 'sample.wav').write_bytes(reference_wav())
    db.add(VoiceProfile(id='voice', name='Voice'))
    db.add_all([
        ProfileSample(id='one', profile_id='voice', audio_path='one/sample.wav', reference_text='One'),
        ProfileSample(id='two', profile_id='voice', audio_path='two/sample.wav', reference_text='Two'),
    ])
    db.commit()
    result = await export_import.import_profile_from_zip(export_import.export_profile_to_zip('voice', db), db)
    restored = db.query(ProfileSample).filter_by(profile_id=result.id).all()
    assert {sample.reference_text for sample in restored} == {'One', 'Two'}
    assert result.sample_count == 2


@pytest.mark.asyncio
async def test_profile_import_response_includes_successfully_restored_avatar(db, monkeypatch, tmp_path):
    from PIL import Image
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    audio = tmp_path / 'sample.wav'
    audio.write_bytes(reference_wav())
    Image.new('RGB', (32, 32), 'red').save(tmp_path / 'avatar.png')
    db.add(VoiceProfile(id='voice', name='Voice', avatar_path='avatar.png'))
    db.add(ProfileSample(id='sample', profile_id='voice', audio_path='sample.wav', reference_text='Hello'))
    db.commit()
    result = await export_import.import_profile_from_zip(export_import.export_profile_to_zip('voice', db), db)
    assert result.avatar_path is not None
    assert result.avatar_path == db.get(VoiceProfile, result.id).avatar_path
    assert config.resolve_storage_path(result.avatar_path).is_file()
    assert result.sample_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('profile_fields', [
    {'voice_type': 'preset'},
    {'voice_type': 'designed'},
    {'voice_type': 'cloned', 'preset_engine': 'kokoro', 'preset_voice_id': 'af_heart'},
    {'voice_type': 'cloned', 'default_engine': 'qwen_voice_design'},
    {'voice_type': 'unknown'},
    {'effects_chain': {'type': 'gain'}},
    {'effects_chain': [{'params': {}}]},
    {'personality': 'x' * 2001},
])
async def test_invalid_profile_archive_settings_leave_no_profile_or_files(profile_fields, db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    data = archive({'version': '1.0', 'profile': {'name': 'Bad', **profile_fields}}, {})
    with pytest.raises(ValueError):
        await export_import.import_profile_from_zip(data, db)
    assert db.query(VoiceProfile).count() == 0
    assert db.query(ProfileSample).count() == 0
    assert list(config.get_profiles_dir().iterdir()) == []


@pytest.mark.asyncio
async def test_legacy_profile_archive_without_new_fields_imports_as_cloned(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    data = archive({'version': '1.0', 'profile': {'name': 'Legacy', 'language': 'en'}}, {})
    result = await export_import.import_profile_from_zip(data, db)
    restored = db.get(VoiceProfile, result.id)
    assert restored.voice_type == 'cloned'
    assert restored.effects_chain is None
    assert restored.default_engine is None
    assert result.sample_count == 0


@pytest.mark.asyncio
async def test_empty_effects_chain_survives_sample_free_profile_roundtrip(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice', voice_type='preset', preset_engine='kokoro',
                        preset_voice_id='af_heart', default_engine='kokoro', effects_chain='[]'))
    db.commit()
    result = await export_import.import_profile_from_zip(export_import.export_profile_to_zip('voice', db), db)
    assert json.loads(db.get(VoiceProfile, result.id).effects_chain) == []
    assert result.effects_chain == []


def test_cloned_profile_without_samples_still_cannot_export(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    db.add(VoiceProfile(id='voice', name='Voice', voice_type='cloned'))
    db.commit()
    with pytest.raises(ValueError, match='no samples'):
        export_import.export_profile_to_zip('voice', db)
