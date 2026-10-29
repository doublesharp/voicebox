import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).resolve().parents[1] / 'rust-production-coverage.py'
spec = importlib.util.spec_from_file_location('rust_production_coverage', path)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_summarizes_only_production_lines_and_preserves_zero_coverage(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    source = root / 'main.rs'
    source.write_text('fn main() {}\n#[cfg(test)]\nmod tests {}\n')
    lcov = tmp_path / 'coverage.lcov'
    lcov.write_text(f'SF:{source}\nDA:1,0\nDA:2,1\nDA:3,1\nend_of_record\n')
    monkeypatch.setattr(report.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout=f'{source}\t2\t3\n'))
    result = report.summarize(lcov, root, Path('/parser'))
    assert (result['covered'], result['total'], result['percent']) == (0, 1, 0.0)
    assert result['excluded_test_lines'] == 2
    assert result['files'][0]['file'] == 'main.rs'
    assert json.loads(json.dumps(result)) == result


def test_rejects_source_outside_checkout(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    source = tmp_path / 'foreign.rs'
    source.write_text('fn main() {}')
    lcov = tmp_path / 'coverage.lcov'
    lcov.write_text(f'SF:{source}\nDA:1,1\nend_of_record\n')
    with pytest.raises(ValueError, match='outside the checkout'):
        report.summarize(lcov, root, Path('/parser'))


def test_rejects_duplicate_line_records(tmp_path):
    source = tmp_path / 'main.rs'
    source.write_text('fn main() {}')
    lcov = tmp_path / 'coverage.lcov'
    lcov.write_text(f'SF:{source}\nDA:1,1\nDA:1,0\nend_of_record\n')
    with pytest.raises(ValueError, match='Duplicate LCOV line'):
        report.summarize(lcov, tmp_path, Path('/parser'))


def test_rejects_unclassified_test_only_syntax(tmp_path, monkeypatch):
    source = tmp_path / 'main.rs'
    source.write_text('#[cfg(test)]\nmod tests {}\n')
    lcov = tmp_path / 'coverage.lcov'
    lcov.write_text(f'SF:{source}\nDA:2,1\nend_of_record\n')
    monkeypatch.setattr(report.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=''))
    with pytest.raises(ValueError, match='Unclassified test-only cfg'):
        report.summarize(lcov, tmp_path, Path('/parser'))
