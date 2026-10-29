"""Exercise the application factory and lifecycle without starting models."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.mcp_server import server as mcp_server


@pytest.fixture
def application(monkeypatch):
    events = []

    @asynccontextmanager
    async def mcp_lifespan(_app):
        events.append('mcp-start')
        yield
        events.append('mcp-stop')

    mcp_app = FastAPI(lifespan=mcp_lifespan)
    fake_mcp = SimpleNamespace(http_app=lambda **_kwargs: mcp_app)
    monkeypatch.setattr(mcp_server, 'build_mcp_server', lambda: fake_mcp)
    application = app_module.create_app()
    application.state.studio_runtime = SimpleNamespace(
        initialize=lambda: events.append('studio-start'),
        jobs=SimpleNamespace(interrupt_on_startup=lambda: events.append('interrupt-jobs')),
    )
    return application, events


@pytest.mark.asyncio
async def test_lifecycle_starts_studio_before_mcp_and_shuts_mcp_before_models(application, monkeypatch):
    app, events = application
    async def startup(_app):
        events.append('startup')
    async def shutdown():
        events.append('shutdown')
    monkeypatch.setattr(app_module, '_run_startup', startup)
    monkeypatch.setattr(app_module, '_run_shutdown', shutdown)
    async with app.router.lifespan_context(app):
        assert events == ['startup', 'studio-start', 'interrupt-jobs', 'mcp-start']
    assert events == ['startup', 'studio-start', 'interrupt-jobs', 'mcp-start', 'mcp-stop', 'shutdown']


@pytest.mark.asyncio
async def test_failed_startup_still_releases_inference_resources(application, monkeypatch):
    app, events = application
    from backend.utils import inference
    monkeypatch.setattr(inference, 'start_inference', lambda: events.append('inference-start'))
    def fail_db():
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(app_module.database, 'init_db', fail_db)
    shutdown = AsyncMock()
    monkeypatch.setattr(app_module, '_run_shutdown', shutdown)
    with pytest.raises(RuntimeError, match='database unavailable'):
        async with app.router.lifespan_context(app):
            pytest.fail('startup must not succeed')
    assert events == ['inference-start']
    shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_studio_startup_failure_shuts_down_models(application, monkeypatch):
    app, _ = application
    monkeypatch.setattr(app_module, '_run_startup', AsyncMock())
    shutdown = AsyncMock()
    monkeypatch.setattr(app_module, '_run_shutdown', shutdown)
    def fail():
        raise RuntimeError('studio unavailable')
    app.state.studio_runtime.initialize = fail
    with pytest.raises(RuntimeError, match='studio unavailable'):
        async with app.router.lifespan_context(app):
            pytest.fail('startup must not succeed')
    shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_unloads_remaining_models_even_if_one_fails(monkeypatch):
    calls = []
    def fail():
        calls.append('tts')
        raise RuntimeError('unload failed')
    monkeypatch.setattr(app_module.tts, 'unload_tts_model', fail)
    monkeypatch.setattr(app_module.transcribe, 'unload_whisper_model', lambda: calls.append('stt'))
    async def unload_llm():
        calls.append('llm')
    monkeypatch.setattr(app_module.llm, 'unload_llm_model_async', unload_llm)
    await app_module._unload_models()
    assert calls == ['tts', 'stt', 'llm']


@pytest.mark.parametrize('origin, allowed', [
    ('http://localhost:5173', True),
    ('http://tauri.localhost', True),
    ('https://custom.example', True),
    ('https://attacker.example', False),
])
def test_real_cors_configuration_enforces_origins(monkeypatch, origin, allowed):
    monkeypatch.setenv('VOICEBOX_CORS_ORIGINS', ' https://custom.example, ')
    app = FastAPI()
    app_module._configure_cors(app)
    @app.get('/health')
    async def health():
        return {'ok': True}
    response = TestClient(app).get('/health', headers={'Origin': origin})
    assert response.status_code == 200
    assert (response.headers.get('access-control-allow-origin') == origin) is allowed


@pytest.mark.asyncio
async def test_mcp_startup_failure_still_shuts_down_voicebox(monkeypatch):
    @asynccontextmanager
    async def broken_mcp(_app):
        raise RuntimeError('MCP startup failed')
        yield
    fake_mcp = SimpleNamespace(http_app=lambda **_kwargs: FastAPI(lifespan=broken_mcp))
    monkeypatch.setattr(mcp_server, 'build_mcp_server', lambda: fake_mcp)
    app = app_module.create_app()
    app.state.studio_runtime = SimpleNamespace(
        initialize=lambda: None,
        jobs=SimpleNamespace(interrupt_on_startup=lambda: None),
    )
    monkeypatch.setattr(app_module, '_run_startup', AsyncMock())
    shutdown = AsyncMock()
    monkeypatch.setattr(app_module, '_run_shutdown', shutdown)
    with pytest.raises(RuntimeError, match='MCP startup failed'):
        async with app.router.lifespan_context(app):
            pytest.fail('startup must not succeed')
    shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_marks_only_incomplete_generations_failed(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.database import Base, Generation, VoiceProfile
    from backend.services import settings
    from backend.utils import inference
    from backend.backends import base
    from huggingface_hub import constants

    engine = create_engine(f'sqlite:///{tmp_path / "startup.db"}')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(VoiceProfile(id='voice', name='Narrator'))
        db.add_all([
            Generation(id='generating', profile_id='voice', text='one', status='generating'),
            Generation(id='loading', profile_id='voice', text='two', status='loading_model'),
            Generation(id='complete', profile_id='voice', text='three', status='completed'),
        ])
        db.commit()
    monkeypatch.setattr(app_module.database, 'init_db', lambda: None)
    monkeypatch.setattr(app_module, 'get_db', lambda: iter([sessions()]))
    monkeypatch.setattr(app_module.config, 'get_data_dir', lambda: tmp_path)
    monkeypatch.setattr(inference, 'start_inference', lambda: None)
    monkeypatch.setattr(app_module, 'init_queue', lambda: None)
    monkeypatch.setattr(app_module, 'get_progress_manager', lambda: SimpleNamespace(_set_main_loop=lambda _loop: None))
    monkeypatch.setattr(app_module, 'get_backend_type', lambda: 'cpu')
    monkeypatch.setattr(app_module, '_get_gpu_status', lambda: 'None (CPU only)')
    monkeypatch.setattr(base, 'check_cuda_compatibility', lambda: (True, None))
    monkeypatch.setattr(settings, 'bootstrap_llm_backend_config', lambda _db: None)
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(tmp_path / 'model-cache'))
    queued = []
    def record_task(coro):
        queued.append(coro.cr_code.co_name)
        coro.close()
    monkeypatch.setattr(app_module, 'create_background_task', record_task)
    try:
        await app_module._run_startup(FastAPI())
        with sessions() as db:
            assert db.get(Generation, 'generating').status == 'failed'
            assert db.get(Generation, 'loading').status == 'failed'
            assert db.get(Generation, 'complete').status == 'completed'
            assert 'shut down' in db.get(Generation, 'generating').error
        assert set(queued) == {'check_and_update_cuda_binary', 'check_and_update_rocm_binary'}
        assert (tmp_path / 'model-cache').is_dir()
    finally:
        engine.dispose()
