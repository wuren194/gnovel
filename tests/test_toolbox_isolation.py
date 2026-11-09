"""Real subprocess/kill controls for the mutation-test execution boundary."""
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.toolbox_isolation import copy_toolbox, ISOLATED_ROOT_ENV, MARKER


class ToolboxCopyTests(unittest.TestCase):
    def test_current_dirty_and_untracked_bytes_are_copied_without_shared_inodes(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'; source.mkdir()
            (source / 'tracked.py').write_text('current dirty bytes')
            (source / 'untracked.txt').write_text('new input')
            (source / 'inside').symlink_to('tracked.py')
            target = Path(temp) / 'copy'
            report = copy_toolbox(source, target)
            self.assertEqual((target / 'tracked.py').read_text(), 'current dirty bytes')
            self.assertEqual((target / 'untracked.txt').read_text(), 'new input')
            self.assertEqual((target / 'inside').resolve(), (target / 'tracked.py').resolve())
            self.assertNotEqual((target / 'tracked.py').stat().st_ino, (source / 'tracked.py').stat().st_ino)
            self.assertEqual(set(report['files']), {'tracked.py', 'untracked.txt', 'inside'})
            (target / 'inside').write_text('mutation')
            self.assertEqual((source / 'tracked.py').read_text(), 'current dirty bytes')

    def test_escape_and_destination_inside_source_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'; source.mkdir()
            outside = Path(temp) / 'outside'; outside.write_text('keep')
            (source / 'escape').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                copy_toolbox(source, Path(temp) / 'copy')
            with self.assertRaisesRegex(ValueError, 'outside'):
                copy_toolbox(source, source / 'nested')
            self.assertEqual(outside.read_text(), 'keep')

    def _fixture(self, source, body):
        (source / 'tests').mkdir(parents=True)
        (source / 'tests/__init__.py').write_text('')
        shutil.copyfile(Path(__file__).with_name('toolbox_isolation.py'), source / 'tests/toolbox_isolation.py')
        (source / 'tests/test_probe.py').write_text(
            'from pathlib import Path\nimport unittest,os,json,time,subprocess,sys\n'
            + body + '\nfrom tests.toolbox_isolation import install_isolation\ninstall_isolation(globals())\n')
        (source / 'owned.py').write_text('untouched source')

    def test_result_bridge_preserves_all_ids_failures_errors_and_skips(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            self._fixture(source, '''class Probe(unittest.TestCase):
 def test_ok(self): self.assertTrue(Path(os.environ["GNOVEL_TEST_COPY_ROOT"]).joinpath(".test-toolbox-copy.json").is_file())
 def test_fail(self): self.fail("remote failure detail")
 def test_error(self): raise ValueError("remote error detail")
 @unittest.skip("browser dependency absent")
 def test_skip(self): pass
 @unittest.expectedFailure
 def test_expected(self): self.fail("expected")
 def test_subtest(self):
  with self.subTest(value=1): self.fail("subtest detail")
''')
            script = ('import unittest,json; s=unittest.defaultTestLoader.loadTestsFromName("tests.test_probe"); '
                      'r=unittest.TestResult(); s.run(r); print(json.dumps({"run":r.testsRun,"fail":len(r.failures),'
                      '"error":len(r.errors),"skip":[[t.id(),v] for t,v in r.skipped],'
                      '"expected":len(r.expectedFailures),"details":[v for t,v in r.failures+r.errors]}))')
            proc = subprocess.run([sys.executable, '-c', script], cwd=source,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual((result['run'], result['fail'], result['error'], result['expected']), (6, 2, 1, 1))
            self.assertEqual(result['skip'], [['tests.test_probe.Probe.test_skip', 'browser dependency absent']])
            self.assertTrue(any('remote failure detail' in value for value in result['details']))
            self.assertTrue(any('subtest detail' in value for value in result['details']))
            self.assertEqual((source / 'owned.py').read_text(), 'untouched source')

    def test_sigkill_after_mutation_and_concurrent_author_edit_never_restores_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'; event = Path(temp) / 'ready.json'
            self._fixture(source, '''class Probe(unittest.TestCase):
 def test_wait(self):
  root=Path(__file__).resolve().parents[1]
  (root/"owned.py").write_text("mutated copy")
  (root/"knowledge/.cache").mkdir(parents=True,exist_ok=True)
  (root/"knowledge/.cache/search.sqlite3").write_bytes(b"copied index")
  child=subprocess.run([sys.executable,"-c","from pathlib import Path; import tests.test_probe as m; print(Path(m.__file__).resolve())"],cwd=root,capture_output=True,text=True,check=True)
  Path(os.environ["ISOLATION_EVENT"]).write_text(json.dumps({"root":str(root),"child":child.stdout.strip()}))
  while True: time.sleep(.1)
''')
            (source / '.gitignore').write_text('knowledge/.cache/\n')
            (source / 'knowledge/.cache').mkdir(parents=True)
            sentinel = source / 'knowledge/.cache/search.sqlite3'; sentinel.write_bytes(b'ignored source sentinel')
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            subprocess.run(['git', 'add', 'owned.py', 'tests', '.gitignore'], cwd=source, check=True)
            (source / 'untracked.txt').write_text('untracked sentinel')
            process = subprocess.Popen([sys.executable, '-m', 'unittest', 'tests.test_probe'], cwd=source,
                                       env={**os.environ, 'ISOLATION_EVENT': str(event)}, start_new_session=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 15
                while not event.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(.01)
                self.assertTrue(event.exists(), 'mutation barrier was never reached')
                report = json.loads(event.read_text()); copy = Path(report['root'])
                self.assertNotEqual(copy, source)
                self.assertTrue(Path(report['child']).is_relative_to(copy))
                self.assertEqual((copy / 'owned.py').read_text(), 'mutated copy')
                (source / 'owned.py').write_text('new concurrent author bytes')
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
                self.assertEqual((source / 'owned.py').read_text(), 'new concurrent author bytes')
                self.assertEqual((source / 'untracked.txt').read_text(), 'untracked sentinel')
                self.assertEqual(sentinel.read_bytes(), b'ignored source sentinel')
                self.assertEqual((copy / 'knowledge/.cache/search.sqlite3').read_bytes(), b'copied index')
                shutil.rmtree(copy.parent)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)

    def test_two_copies_have_separate_catalog_index_and_history(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'; source.mkdir()
            (source / 'knowledge').mkdir(); (source / 'knowledge/catalog.json').write_text('{}')
            copies = [Path(temp) / name for name in ('one', 'two')]
            for index, copy in enumerate(copies):
                copy_toolbox(source, copy)
                for relative in ('knowledge/catalog.json', 'knowledge/.cache/search.sqlite3', 'knowledge/.history/item'):
                    path = copy / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(str(index))
            self.assertEqual((source / 'knowledge/catalog.json').read_text(), '{}')
            for index, copy in enumerate(copies):
                self.assertEqual((copy / 'knowledge/.cache/search.sqlite3').read_text(), str(index))
                self.assertEqual((copy / 'knowledge/.history/item').read_text(), str(index))

    def test_actual_manifest_guard_fails_for_unconsumed_field_in_physical_copy(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / 'toolbox'
            copy_toolbox(source, copy)
            manifest = copy / 'toolbox.manifest.json'
            original = json.loads(manifest.read_text())
            mutant = {**original, 'unconsumedManifestProbe': {'cache': 'same leaf is not consumption'}}
            manifest.write_text(json.dumps(mutant, ensure_ascii=False))
            command = [sys.executable, '-m', 'unittest',
                       'tests.test_single_source_fields.ManifestSingleSourceTests.test_no_phantom_fields']
            result = subprocess.run(command, cwd=copy, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unconsumedManifestProbe.cache', result.stderr)
            manifest.write_text(json.dumps(original, ensure_ascii=False))
            result = subprocess.run(command, cwd=copy, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
