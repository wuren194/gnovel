"""The cheaper CLI harness preserves boundary behavior and clears process caches."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.cli_harness import run_cli


class CliHarnessTests(unittest.TestCase):
    def test_repeated_calls_clear_all_product_caches_and_restore_cwd(self):
        from scripts.core import static_cache, knowledge_store, knowledge_index, history_index
        caches = [static_cache._FILES, static_cache._VALUES, knowledge_store._ASSET_ROWS,
                  knowledge_index._INDEX_CACHE, history_index._CACHE]
        previous = Path.cwd()
        def main(argv):
            self.assertTrue(all(not cache for cache in caches))
            for cache in caches: cache['probe'] = object()
            print('{"ok": true}')
            return 0
        with tempfile.TemporaryDirectory() as temp, patch('scripts.novelctl.main', side_effect=main):
            for _ in range(2):
                result = run_cli(['project', 'show'], cwd=Path(temp))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '{"ok": true}\n')
                self.assertEqual(Path.cwd(), previous)
                self.assertTrue(all(not cache for cache in caches))

    def test_parser_exit_and_stderr_are_preserved(self):
        result = run_cli(['not-a-command'])
        actual = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/novelctl.py'),
                                 'not-a-command'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.returncode, actual.returncode)
        self.assertEqual(result.stderr, actual.stderr)
        self.assertEqual(json.loads(result.stdout), json.loads(actual.stdout))
        self.assertEqual(json.loads(result.stdout)['code'], 'ARGUMENTS_INVALID')
        self.assertIn('invalid choice', json.loads(result.stdout)['error'])

    def test_exception_restores_cwd_and_does_not_hide_failure(self):
        from scripts.core import knowledge_store
        previous = Path.cwd()
        def explode(argv):
            knowledge_store._ASSET_ROWS['probe'] = object()
            raise RuntimeError('harness probe')
        with tempfile.TemporaryDirectory() as temp, patch('scripts.novelctl.main', side_effect=explode):
            with self.assertRaisesRegex(RuntimeError, 'harness probe'):
                run_cli([], cwd=Path(temp))
        self.assertEqual(Path.cwd(), previous)
        self.assertFalse(knowledge_store._ASSET_ROWS)
