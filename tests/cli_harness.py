"""In-process CLI calls for behavior tests; OS-boundary tests keep subprocesses."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
CACHES = {
    'core.static_cache': ('_FILES', '_VALUES'),
    'core.knowledge_store': ('_ASSET_ROWS',),
    'core.knowledge_index': ('_INDEX_CACHE',),
    'core.history_index': ('_CACHE',),
}


def clear_runtime_caches():
    """Both script-style and package imports occur in the supported CLI facade."""
    for prefix in ('', 'scripts.'):
        for name, fields in CACHES.items():
            module = sys.modules.get(prefix + name)
            if module is not None:
                for field in fields:
                    getattr(module, field).clear()
        for name, function in (('intensity_registry', 'load'), ('text.ai_patterns', 'load_text_patterns')):
            module = sys.modules.get(prefix + name)
            if module is not None:
                clear = getattr(getattr(module, function), 'cache_clear', None)
                if clear is not None:
                    clear()


def run_cli(argv, *, cwd=None):
    from scripts.novelctl import main
    previous = Path.cwd()
    previous_path = list(sys.path)
    stdout, stderr = io.StringIO(), io.StringIO()
    clear_runtime_caches()
    try:
        os.chdir(cwd or ROOT)
        sys.path.insert(0, str(ROOT / 'scripts'))
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(previous)
        sys.path[:] = previous_path
        clear_runtime_caches()
    return SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def cli_json(argv, *, cwd=None):
    result = run_cli(argv, cwd=cwd)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {'code': 'NON_JSON', 'stdout': result.stdout, 'stderr': result.stderr}
    return result.returncode, payload
