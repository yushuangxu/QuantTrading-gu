"""Explicit offline test entrypoints; never discover root manual/network tools.

regression = all supported tests minus the EXACT documented nine issue IDs.
known = those nine ordinary assertions; current exit code is intentionally 1.
Unexpected regression failures are never reclassified as known issues.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def partition():
    declared = json.loads((ROOT / 'tests' / 'known_issues.json').read_text(encoding='utf-8'))
    if len(declared) != 9 or len(set(declared)) != 9:
        raise ValueError('Known-issue manifest must contain exactly nine distinct IDs')
    tests = list(flatten(unittest.TestLoader().discover(str(ROOT / 'tests'), pattern='test_*.py')))
    ids = [test.id() for test in tests]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate test identities; refusing silent double execution')
    missing = set(declared) - set(ids)
    if missing:
        raise ValueError(f'Missing known-issue tests: {sorted(missing)}')
    known = [test for test in tests if test.id() in declared]
    normal = [test for test in tests if test.id() not in declared]
    return normal, known


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite', choices=('regression', 'known', 'all'))
    args = parser.parse_args(argv)
    # Fail clearly if dependencies are absent; do not install, skip or borrow
    # packages from another interpreter behind the user's back.
    import numpy
    import pandas
    import flask
    from importlib.metadata import version
    print(f'Python {sys.version.split()[0]} | numpy {numpy.__version__} | '
          f'pandas {pandas.__version__} | Flask {version("flask")}', flush=True)
    normal, known = partition()
    selected = {'regression': normal, 'known': known, 'all': normal + known}[args.suite]
    print(f'Suite={args.suite}: {len(selected)} tests; regression={len(normal)}, known={len(known)}', flush=True)
    if args.suite == 'known':
        print('Ordinary failing assertions are expected while defects remain; exit 1 is NOT masked.', flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
