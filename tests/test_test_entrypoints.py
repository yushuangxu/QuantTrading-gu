"""Partition integrity guards: known failures cannot swallow new regressions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_tests import flatten, partition


class TestEntrypoints(unittest.TestCase):
    def test_partition_is_exhaustive_disjoint_and_known_count_is_nine(self):
        normal, known = partition()
        normal_ids, known_ids = {t.id() for t in normal}, {t.id() for t in known}
        self.assertEqual(len(known), 9)
        self.assertFalse(normal_ids & known_ids)
        all_tests = list(flatten(unittest.TestLoader().discover(str(Path(__file__).parent), pattern='test_*.py')))
        self.assertEqual(normal_ids | known_ids, {t.id() for t in all_tests})

    def test_known_issues_are_not_skipped_or_expected_failures(self):
        _, known = partition()
        for test in known:
            method = getattr(test, test._testMethodName)
            self.assertFalse(getattr(test, '__unittest_skip__', False))
            self.assertFalse(getattr(method, '__unittest_skip__', False))
            self.assertFalse(getattr(method, '__unittest_expecting_failure__', False))

    def test_missing_known_test_is_an_error_not_silent_removal(self):
        with patch('run_tests.unittest.TestLoader.discover', return_value=unittest.TestSuite()):
            with self.assertRaisesRegex(ValueError, 'Missing known-issue'):
                partition()
