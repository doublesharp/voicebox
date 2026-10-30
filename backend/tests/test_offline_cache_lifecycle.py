"""Cached model loading setup and failure recovery."""

import builtins
import os
import threading

import pytest

from backend.utils import hf_offline_patch as patch


@pytest.mark.parametrize('attribute', ['_try_to_load_from_cache', 'try_to_load_from_cache'])
def test_huggingface_cache_patch_is_idempotent_and_preserves_lookup_arguments(monkeypatch, attribute):
    import huggingface_hub.file_download as download

    calls = []
    def lookup(**kwargs):
        calls.append(kwargs)
        return '/cache/model.bin'
    monkeypatch.delattr(download, '_try_to_load_from_cache', raising=False)
    monkeypatch.setattr(download, attribute, lookup, raising=False)
    patch.patch_huggingface_hub_offline()
    first = getattr(download, attribute)
    assert first is not lookup
    patch.patch_huggingface_hub_offline()
    assert getattr(download, attribute) is first
    assert first('author/model', 'model.bin', '/cache', 'revision', 'model') == '/cache/model.bin'
    assert calls == [{'repo_id': 'author/model', 'filename': 'model.bin', 'cache_dir': '/cache',
                      'revision': 'revision', 'repo_type': 'model'}]


def test_cache_patch_preserves_misses_and_lookup_errors(monkeypatch):
    import huggingface_hub.file_download as download
    monkeypatch.delattr(download, '_try_to_load_from_cache', raising=False)
    monkeypatch.setattr(download, 'try_to_load_from_cache', lambda **_kwargs: None)
    patch.patch_huggingface_hub_offline()
    assert download.try_to_load_from_cache('author/model', 'missing.bin') is None
    def fail(**_kwargs):
        raise OSError('cache unavailable')
    monkeypatch.setattr(download, 'try_to_load_from_cache', fail)
    patch.patch_huggingface_hub_offline()
    with pytest.raises(OSError, match='cache unavailable'):
        download.try_to_load_from_cache('author/model', 'missing.bin')


def test_concurrent_cache_initialization_installs_one_wrapper(monkeypatch):
    import huggingface_hub.file_download as download
    monkeypatch.delattr(download, '_try_to_load_from_cache', raising=False)
    calls = []
    def lookup(**kwargs):
        calls.append(kwargs)
        return 'cached'
    monkeypatch.setattr(download, 'try_to_load_from_cache', lookup)
    barrier = threading.Barrier(8)
    def initialize():
        barrier.wait(timeout=5)
        patch.patch_huggingface_hub_offline()
    threads = [threading.Thread(target=initialize) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    wrapper = download.try_to_load_from_cache
    patch.patch_huggingface_hub_offline()
    assert download.try_to_load_from_cache is wrapper
    assert wrapper('author/model', 'model.bin') == 'cached'
    assert len(calls) == 1


def test_missing_cache_lookup_is_nonfatal(monkeypatch):
    import huggingface_hub.file_download as download
    monkeypatch.delattr(download, '_try_to_load_from_cache', raising=False)
    monkeypatch.delattr(download, 'try_to_load_from_cache', raising=False)
    patch.patch_huggingface_hub_offline()


def test_cached_mlx_model_creates_original_repo_alias_without_replacing_existing_cache(monkeypatch, tmp_path):
    import huggingface_hub.constants as constants
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(tmp_path))
    original = tmp_path / 'models--Qwen--Qwen3-TTS-12Hz-1.7B-Base'
    mlx = tmp_path / 'models--mlx-community--Qwen3-TTS-12Hz-1.7B-Base-bf16'
    patch.ensure_original_qwen_config_cached()
    assert not original.exists()
    mlx.mkdir()
    (mlx / 'config.json').write_text('cached')
    patch.ensure_original_qwen_config_cached()
    assert original.is_symlink()
    assert (original / 'config.json').read_text() == 'cached'
    original.unlink()
    original.mkdir()
    (original / 'config.json').write_text('existing')
    patch.ensure_original_qwen_config_cached()
    assert not original.is_symlink()
    assert (original / 'config.json').read_text() == 'existing'


def test_cache_alias_failure_is_nonfatal(monkeypatch, tmp_path):
    import huggingface_hub.constants as constants
    from pathlib import Path
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(tmp_path))
    (tmp_path / 'models--mlx-community--Qwen3-TTS-12Hz-1.7B-Base-bf16').mkdir()
    def deny(*_args, **_kwargs):
        raise PermissionError('symlinks unavailable')
    monkeypatch.setattr(Path, 'symlink_to', deny)
    patch.ensure_original_qwen_config_cached()
    assert not (tmp_path / 'models--Qwen--Qwen3-TTS-12Hz-1.7B-Base').exists()


@pytest.mark.parametrize('prior_env', [None, '0', '1'])
def test_failed_offline_initialization_restores_previous_state(monkeypatch, prior_env):
    import huggingface_hub.constants as constants
    import transformers.utils.hub as transformers_hub

    if prior_env is None:
        monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
    else:
        monkeypatch.setenv('HF_HUB_OFFLINE', prior_env)
    old_hf = constants.HF_HUB_OFFLINE
    old_tf = transformers_hub._is_offline_mode
    real_import = builtins.__import__
    def fail_transformers(name, *args, **kwargs):
        if name == 'transformers.utils.hub':
            raise RuntimeError('transformers initialization failed')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', fail_transformers)
    with pytest.raises(RuntimeError, match='transformers initialization failed'):
        with patch.force_offline_if_cached(True, 'cached'):
            pytest.fail('failed setup must not enter the model load')
    assert constants.HF_HUB_OFFLINE == old_hf
    assert transformers_hub._is_offline_mode == old_tf
    assert os.environ.get('HF_HUB_OFFLINE') == prior_env
    assert patch._offline_refcount == 0
    # The guard must be usable after failed initialization.
    monkeypatch.setattr(builtins, '__import__', real_import)
    with patch.force_offline_if_cached(True, 'retry'):
        assert constants.HF_HUB_OFFLINE is True


@pytest.mark.parametrize('cached', [True, False])
def test_exception_during_model_load_releases_guard_and_restores_environment(monkeypatch, cached):
    import huggingface_hub.constants as constants
    import transformers.utils.hub as transformers_hub
    monkeypatch.setenv('HF_HUB_OFFLINE', '0')
    before_hf = constants.HF_HUB_OFFLINE
    before_tf = transformers_hub._is_offline_mode
    with pytest.raises(ValueError, match='load failed'):
        with patch.force_offline_if_cached(cached, 'model'):
            raise ValueError('load failed')
    assert os.environ['HF_HUB_OFFLINE'] == '0'
    assert constants.HF_HUB_OFFLINE == before_hf
    assert transformers_hub._is_offline_mode == before_tf
    assert patch._offline_refcount == 0
    assert patch._uncached_active == 0
    with patch.force_offline_if_cached(not cached, 'next model'):
        pass
