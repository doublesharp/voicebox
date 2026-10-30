"""Download progress aggregation and patch lifetime behavior."""
import io

import pytest
import tqdm
import tqdm.auto

from backend.utils.hf_progress import HFProgressTracker, create_hf_progress_callback


def test_download_import_error_is_preserved_and_patches_are_restored():
    original, original_auto = tqdm.tqdm, tqdm.auto.tqdm
    with pytest.raises(ImportError, match='missing model dependency'):
        with HFProgressTracker().patch_download():
            assert tqdm.tqdm is not original
            raise ImportError('missing model dependency')
    assert tqdm.tqdm is original
    assert tqdm.auto.tqdm is original_auto


def test_patches_restore_after_other_download_failure():
    original, original_auto = tqdm.tqdm, tqdm.auto.tqdm
    with pytest.raises(RuntimeError, match='download failed'):
        with HFProgressTracker().patch_download():
            raise RuntimeError('download failed')
    assert tqdm.tqdm is original
    assert tqdm.auto.tqdm is original_auto


def test_aggregate_counts_file_downloads_and_ignores_file_count_bars():
    events = []
    tracker = HFProgressTracker(lambda downloaded, total, filename: events.append((downloaded, total, filename)))
    tracker._original_tqdm_class = tqdm.tqdm
    cls = tracker._create_tracked_tqdm_class()
    with cls(total=2_000_000, desc='model.bin', file=io.StringIO()) as first:
        first.update(500_000)
        with cls(total=1_000_000, desc='config.json', file=io.StringIO()) as second:
            second.update(250_000)
            with cls(total=2_000_000, desc='Fetching 2 files', file=io.StringIO()) as fetching:
                fetching.update(1)
            first.update(250_000)
    assert events == [(500_000, 2_000_000, 'model.bin'), (750_000, 3_000_000, 'config.json'), (1_000_000, 3_000_000, 'model.bin')]
    assert tracker._active_tqdms == {}


def test_small_files_are_aggregated_without_premature_callback():
    events = []
    tracker = HFProgressTracker(lambda *args: events.append(args))
    tracker._original_tqdm_class = tqdm.tqdm
    cls = tracker._create_tracked_tqdm_class()
    with cls(total=400_000, desc='config.json', file=io.StringIO()) as first:
        first.update(400_000)
    assert events == []
    with cls(total=1_000_000, desc='model.bin', file=io.StringIO()) as second:
        second.update(250_000)
    assert events == [(650_000, 1_400_000, 'model.bin')]


@pytest.mark.parametrize('name,expected', [('model.safetensors', True), ('CONFIG.JSON', True), ('loading model.bin', False), ('Generating output', False), ('unknown', False), ('', False)])
def test_download_filter_excludes_processing_progress(name, expected):
    events = []
    tracker = HFProgressTracker(lambda *args: events.append(args), filter_non_downloads=True)
    tracker._original_tqdm_class = tqdm.tqdm
    cls = tracker._create_tracked_tqdm_class()
    with cls(total=2_000_000, desc=name, file=io.StringIO()) as bar:
        bar.update(100)
    assert bool(events) is expected


def test_callback_adapts_to_progress_manager():
    calls = []
    class Manager:
        def update_progress(self, **kwargs):
            calls.append(kwargs)
    callback = create_hf_progress_callback('voice-model', Manager())
    callback(123, 0)
    assert calls == [{'model_name': 'voice-model', 'current': 123, 'total': 0, 'filename': '', 'status': 'downloading'}]


@pytest.mark.parametrize('failure_step', ['start', 'progress'])
def test_model_download_setup_failure_restores_progress_patch(monkeypatch, failure_step):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from backend.backends import base

    lifecycle = []
    @contextmanager
    def patch():
        lifecycle.append('entered')
        try:
            yield
        finally:
            lifecycle.append('restored')
    def start(_name):
        if failure_step == 'start':
            raise RuntimeError('setup failed')
    def update_progress(**_kwargs):
        if failure_step == 'progress':
            raise RuntimeError('setup failed')
    monkeypatch.setattr(base, 'HFProgressTracker', lambda *args, **kwargs: SimpleNamespace(patch_download=patch))
    monkeypatch.setattr(base, 'get_task_manager', lambda: SimpleNamespace(start_download=start))
    monkeypatch.setattr(base, 'get_progress_manager', lambda: SimpleNamespace(update_progress=update_progress))
    with pytest.raises(RuntimeError, match='setup failed') as captured:
        with base.model_load_progress('voice', is_cached=False):
            pytest.fail('model loading should not start after setup failure')
    assert captured.value.__traceback__ is not None
    assert lifecycle == ['entered', 'restored']


def test_missing_tqdm_falls_back_and_does_not_mask_download_error(monkeypatch):
    import builtins
    real_import = builtins.__import__
    def import_without_tqdm(name, *args, **kwargs):
        if name == 'tqdm':
            raise ImportError('tqdm unavailable')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', import_without_tqdm)
    tracker = HFProgressTracker()
    with tracker.patch_download():
        pass
    with pytest.raises(ImportError, match='model dependency missing'):
        with tracker.patch_download():
            raise ImportError('model dependency missing')


@pytest.mark.parametrize('cached,fail', [(False, False), (False, True), (True, False), (True, True)])
def test_model_load_reports_completion_and_failure_with_patch_cleanup(monkeypatch, cached, fail):
    from contextlib import contextmanager, nullcontext
    from types import SimpleNamespace
    from backend.backends import base
    lifecycle = []
    @contextmanager
    def patch():
        lifecycle.append('patch')
        try:
            yield
        finally:
            lifecycle.append('restore')
    class Manager:
        def __getattr__(self, name):
            return lambda *args, **kwargs: lifecycle.append((name, args, kwargs))
    manager = Manager()
    monkeypatch.setattr(base, 'HFProgressTracker', lambda *args, **kwargs: SimpleNamespace(patch_download=patch))
    monkeypatch.setattr(base, 'get_task_manager', lambda: manager)
    monkeypatch.setattr(base, 'get_progress_manager', lambda: manager)
    monkeypatch.setattr(base, 'force_offline_if_cached', lambda *_args: nullcontext())
    if fail:
        with pytest.raises(ImportError, match='missing engine'):
            with base.model_load_progress('voice', is_cached=cached):
                raise ImportError('missing engine')
    else:
        with base.model_load_progress('voice', is_cached=cached):
            lifecycle.append('load')
    names = [item[0] for item in lifecycle if isinstance(item, tuple)]
    assert lifecycle[0] == 'patch' and lifecycle[-1] == 'restore'
    assert ('start_download' in names) is not cached
    assert ('mark_complete' in names) is (not cached and not fail)
    assert ('complete_download' in names) is (not cached and not fail)
    assert ('mark_error' in names) is fail
    assert ('error_download' in names) is fail
