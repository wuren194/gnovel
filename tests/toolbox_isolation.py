"""Run toolbox-mutating test modules in physical, disposable worktree copies.

The unittest suite still exposes and reports every original test ID. Only its
execution crosses the process boundary; product imports, CLI children, cwd and
knowledge stores all belong to the copy. No cleanup ever writes to the source.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ISOLATED_ROOT_ENV = 'GNOVEL_TEST_COPY_ROOT'
MARKER = '.test-toolbox-copy.json'


def _files(root):
    if (root / '.git').exists():
        raw = subprocess.check_output(
            ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root)
        return sorted({os.fsdecode(item) for item in raw.split(b'\0') if item and os.fsdecode(item) != MARKER})
    # A copy has no Git metadata. It is still a full source tree, never a HEAD
    # archive; omit only owned runtime outputs and Python caches when nesting.
    ignored = {'.git', '__pycache__', '.pytest_cache', '.cache', '.history'}
    result = []
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in list(dirs):
            path = Path(base) / name
            if path.is_symlink():
                result.append(str(path.relative_to(root)))
                dirs.remove(name)
        result.extend(str((Path(base) / name).relative_to(root)) for name in files
                      if name != MARKER and not name.endswith(('.pyc', '.pyo')))
    return sorted(result)


def _read(path):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode):
        return ('symlink', stat.S_IMODE(before.st_mode), os.fsencode(os.readlink(path)))
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Unsupported source entry: ' + str(path))
    data = path.read_bytes()
    after = path.lstat()
    if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns):
        raise RuntimeError('Source changed while snapshotting: ' + str(path))
    return ('file', stat.S_IMODE(before.st_mode), data)


def copy_toolbox(source, target):
    """Copy current bytes, preserving internal links and rejecting escapes.

    Ignored knowledge indexes/history are deliberately not copied: their paths
    start absent inside the copy and real consumers rebuild them there. Source
    ignored sentinels cannot be restored or removed by this helper.
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    if target == source or target.is_relative_to(source):
        raise ValueError('Test copy must be outside its source tree')
    target.mkdir(parents=True, exist_ok=False)
    snapshots = {}
    paths = _files(source)
    for relative in paths:
        path = source / relative
        value = _read(path)
        if value is None:  # tracked deletion is part of the current worktree
            continue
        kind, mode, data = value
        if path.parent.resolve() != source and not path.parent.resolve().is_relative_to(source):
            raise ValueError('Source parent escapes toolbox: ' + relative)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if kind == 'symlink':
            resolved = path.resolve()
            if not resolved.is_relative_to(source):
                raise ValueError('Source link escapes toolbox: ' + relative)
            # Absolute internal links must become copy-local, too.
            link = os.fsdecode(data)
            if os.path.isabs(link):
                link = os.path.relpath(target / resolved.relative_to(source), destination.parent)
            destination.symlink_to(link)
        else:
            with destination.open('xb') as stream:
                stream.write(data)
            destination.chmod(mode)
            if destination.stat().st_ino == path.stat().st_ino and destination.stat().st_dev == path.stat().st_dev:
                raise ValueError('Test copy shares a source inode: ' + relative)
        snapshots[relative] = {'type': kind, 'mode': mode, 'sha256': hashlib.sha256(data).hexdigest()}
    if paths != _files(source):
        raise RuntimeError('Source path set changed while snapshotting')
    for relative, expected in snapshots.items():
        value = _read(source / relative)
        if value is None or {'type': value[0], 'mode': value[1],
                             'sha256': hashlib.sha256(value[2]).hexdigest()} != expected:
            raise RuntimeError('Source bytes changed while snapshotting: ' + relative)
    report = {'source': str(source), 'files': snapshots,
              'fingerprint': hashlib.sha256(json.dumps(snapshots, sort_keys=True).encode()).hexdigest(),
              'excluded': ['Git metadata', MARKER + ' (test-copy provenance, regenerated)',
                           'Git-ignored runtime files (including knowledge index/history)']}
    (target / MARKER).write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    return report


def require_isolated(module_file):
    root = Path(module_file).resolve().parents[1]
    claimed = os.environ.get(ISOLATED_ROOT_ENV)
    if not claimed or Path(claimed).resolve() != root or not (root / MARKER).is_file():
        raise RuntimeError('Toolbox-mutating tests must run by module through the isolated suite; '
                           'direct class/method execution may not write the developer worktree')


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


class _RemoteTest(unittest.TestCase):
    def __init__(self, test_id):
        super().__init__()
        self.test_id = test_id

    def id(self):
        return self.test_id

    def __str__(self):
        return self.test_id


def _exception(message, failure=False):
    try:
        raise AssertionError(message) if failure else RuntimeError(message)
    except Exception:
        return sys.exc_info()


class IsolatedModuleSuite(unittest.TestSuite):
    def __init__(self, module_name, module_file, suite):
        super().__init__(suite)
        self.module_name = module_name
        self.source = Path(module_file).resolve().parents[1]

    def run(self, result, debug=False):
        tests = {test.id(): test for test in _flatten(self)}
        if len(tests) != self.countTestCases():
            raise RuntimeError('Duplicate test IDs in isolated module')
        with tempfile.TemporaryDirectory(prefix='gnovel-test-copy-') as temporary:
            copy = (Path(temporary) / 'toolbox').resolve()
            copy_toolbox(self.source, copy)
            # Package/doctor checks enumerate the actual worktree through Git.
            # A fresh empty repository supplies that interface without copying
            # history, changing the source index, or committing synthetic bytes.
            subprocess.run(['git', 'init', '-q', str(copy)], check=True, capture_output=True)
            (copy / '.git/info/exclude').write_text(MARKER + '\n', encoding='utf-8')
            output = Path(temporary) / 'results.json'
            env = dict(os.environ, **{ISOLATED_ROOT_ENV: str(copy), 'PYTHONDONTWRITEBYTECODE': '1',
                                     'PYTHONPATH': os.pathsep.join(map(str, (copy, copy / 'scripts', copy / 'tests')))})
            process = subprocess.run([sys.executable, str(copy / 'tests/toolbox_isolation.py'),
                                      self.module_name, str(output)], cwd=copy, env=env,
                                     capture_output=True, text=True)
            if not output.is_file() or output.is_symlink():
                raise RuntimeError('Isolated module produced no result: ' + process.stderr[-8000:])
            report = json.loads(output.read_text(encoding='utf-8'))
            if report.get('testIds') != sorted(tests) or report.get('root') != str(copy):
                raise RuntimeError('Isolated module changed test identity or execution root')
            events = report.get('events', [])
            started = [row['id'] for row in events if row.get('event') == 'startTest']
            stopped = [row['id'] for row in events if row.get('event') == 'stopTest']
            if (sorted(started) != sorted(tests) or sorted(stopped) != sorted(tests)
                    or report.get('testsRun') != len(tests) or report.get('loaderErrors')):
                raise RuntimeError('Isolated execution did not account for every discovered test: '
                                   + json.dumps(report, ensure_ascii=False)[-16000:])
            for event in events:
                test = tests.get(event['id'], _RemoteTest(event['id']))
                name = event['event']
                if name in ('startTest', 'stopTest', 'addSuccess', 'addUnexpectedSuccess'):
                    if event['id'] not in tests:
                        raise RuntimeError('Unknown isolated test ID: ' + event['id'])
                    getattr(result, name)(test)
                elif name == 'addSkip':
                    if event.get('parentId', event['id']) not in tests:
                        raise RuntimeError('Unknown isolated skipped test: ' + event['id'])
                    result.addSkip(test, event['message'])
                elif name in ('addFailure', 'addError', 'addExpectedFailure'):
                    getattr(result, name)(test, _exception(event['message'], name != 'addError'))
                else:
                    raise RuntimeError('Unknown isolated result event: ' + name)
            if process.returncode and report.get('successful'):
                raise RuntimeError('Isolated process failed after reporting success: ' + process.stderr[-8000:])
        return result


def install_isolation(namespace):
    """Install module execution isolation without changing discovery identities."""
    original_setup = namespace.get('setUpModule')

    def set_up():
        require_isolated(namespace['__file__'])
        if original_setup:
            original_setup()

    def load_tests(loader, suite, pattern):
        root = Path(namespace['__file__']).resolve().parents[1]
        claimed = os.environ.get(ISOLATED_ROOT_ENV)
        if claimed and Path(claimed).resolve() == root and (root / MARKER).is_file():
            return suite
        if claimed and (root / MARKER).is_file():
            raise RuntimeError('Isolated module root mismatch; refusing a recursive copy')
        return IsolatedModuleSuite(namespace['__name__'], namespace['__file__'], suite)

    namespace['setUpModule'] = set_up
    namespace['load_tests'] = load_tests


class RecordingResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.events = []

    def _event(self, name, test, message=None):
        event = {'event': name, 'id': test.id()}
        if message is not None:
            event['message'] = message
        if hasattr(test, 'test_case'):
            event['parentId'] = test.test_case.id()
        self.events.append(event)

    def startTest(self, test):
        super().startTest(test); self._event('startTest', test)

    def stopTest(self, test):
        super().stopTest(test); self._event('stopTest', test)

    def addSuccess(self, test):
        super().addSuccess(test); self._event('addSuccess', test)

    def addFailure(self, test, err):
        super().addFailure(test, err); self._event('addFailure', test, self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err); self._event('addError', test, self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason); self._event('addSkip', test, reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._event('addExpectedFailure', test, self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test); self._event('addUnexpectedSuccess', test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._event('addFailure' if issubclass(err[0], test.failureException) else 'addError',
                        subtest, self._exc_info_to_string(err, test))


def main():
    root = Path(__file__).resolve().parents[1]
    require_isolated(__file__)
    sys.path[:0] = [str(root / 'tests'), str(root), str(root / 'scripts')]
    loader = unittest.TestLoader()
    loader.discover(str(root / 'tests'), pattern='test_*.py')
    suite = loader.loadTestsFromName(sys.argv[1])
    ids = sorted(test.id() for test in _flatten(suite))
    result = RecordingResult()
    suite.run(result)
    report = {'root': str(root), 'testIds': ids, 'events': result.events,
              'testsRun': result.testsRun,
              'successful': result.wasSuccessful() and not loader.errors, 'loaderErrors': loader.errors}
    with Path(sys.argv[2]).open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False)
    return 0 if report['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
