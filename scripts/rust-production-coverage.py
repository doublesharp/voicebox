#!/usr/bin/env python3
"""Summarize LCOV for compiled Rust source, excluding cfg(test)-only syntax.

Run rust-test-spans from this checkout against the same source used to produce
LCOV. This report covers only files mapped by that build's platform and targets.
"""
import argparse
import json
import re
from pathlib import Path
import subprocess


def summarize(lcov: Path, root: Path, span_tool: Path) -> dict:
    root = root.resolve()
    files: dict[str, dict[int, int]] = {}
    current = None
    for line in lcov.read_text().splitlines():
        if line.startswith('SF:'):
            source = Path(line[3:]).resolve(strict=True)
            if not source.is_relative_to(root):
                raise ValueError(f'Coverage source is outside the checkout: {source}')
            current = str(source)
            if current in files:
                raise ValueError(f'Duplicate LCOV source record: {source}')
            files[current] = {}
        elif line.startswith('DA:'):
            if current is None:
                raise ValueError('LCOV line record without a source file')
            parts = line[3:].split(',')
            number, count = int(parts[0]), int(parts[1])
            if number in files[current]:
                raise ValueError(f'Duplicate LCOV line record: {current}:{number}')
            files[current][number] = count
        elif line == 'end_of_record':
            current = None
    if not files:
        raise ValueError('No mapped Rust source files found')
    result = subprocess.run([str(span_tool), *files], check=True, capture_output=True, text=True)
    ranges: dict[str, list[tuple[int, int]]] = {name: [] for name in files}
    for line in result.stdout.splitlines():
        path, first, last = line.split('\t')
        if path not in ranges:
            raise ValueError(f'Unexpected source from span tool: {path}')
        first, last = int(first), int(last)
        if first < 1 or last < first:
            raise ValueError(f'Invalid test-only syntax span: {line}')
        ranges[path].append((first, last))
    # Fail closed if a simple test-only cfg was not classified by the syntax tool.
    test_cfg = re.compile(r'#\s*\[\s*cfg\s*\(\s*(?:test\b|all\s*\(\s*test\b)')
    for name in files:
        source = Path(name).read_text()
        for match in test_cfg.finditer(source):
            line = source.count('\n', 0, match.start()) + 1
            if not any(first <= line <= last for first, last in ranges[name]):
                raise ValueError(f'Unclassified test-only cfg: {name}:{line}')
    details = []
    for name, lines in sorted(files.items()):
        included = {n: c for n, c in lines.items()
                    if not any(first <= n <= last for first, last in ranges[name])}
        details.append({'file': str(Path(name).relative_to(root)), 'total': len(included),
                        'covered': sum(count > 0 for count in included.values()),
                        'excluded_test_lines': len(lines) - len(included)})
    total = sum(f['total'] for f in details)
    covered = sum(f['covered'] for f in details)
    if not total:
        raise ValueError('No production lines remain in the coverage report')
    return {'scope': 'LCOV-mapped compiled source; cfg(test)-only syntax excluded',
            'total': total, 'covered': covered, 'percent': round(100 * covered / total, 2),
            'excluded_test_lines': sum(f['excluded_test_lines'] for f in details), 'files': details}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lcov', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--span-tool', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.lcov, args.root, args.span_tool), indent=2))


if __name__ == '__main__':
    main()
