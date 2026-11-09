"""gnovel toolbox tests."""

import os
import tempfile
import atexit
from pathlib import Path
import sys

# Keep direct unittest runs out of the user's diagnostic history as well as
# local_ci workers. Dedicated logging tests supply their own mode and directory.
os.environ["GNOVEL_LOG_MODE"] = "off"

# All subprocesses inherit this process-owned directory. Tests cannot write to
# the user's provider or application data while running the public suite.
_data_directory = tempfile.TemporaryDirectory(prefix='gnovel-test-data-')
atexit.register(_data_directory.cleanup)
os.environ['GNOVEL_DATA_HOME'] = str(Path(_data_directory.name).resolve())
os.environ.pop('GNOVEL_PROVIDER_CONFIG', None)

# Direct unittest discovery can import a product module before this package.
# Rebind only those test-process modules before any test body runs.
for _prefix in ('scripts.core.', 'core.'):
    _store = sys.modules.get(_prefix + 'knowledge_store')
    if _store is not None and hasattr(_store, 'ASSETS'):
        _store.TOOLBOX_ROOT = Path(_data_directory.name).resolve()
        _store.ASSETS = _store.TOOLBOX_ROOT / 'knowledge/assets'
        _store._PROVIDER_BINDING = {'providerId': None, 'version': None, 'sha256': None}
        _store._ASSET_ROWS.clear()
    _index = sys.modules.get(_prefix + 'knowledge_index')
    if _index is not None and hasattr(_index, 'INDEX'):
        _index.TOOLBOX_ROOT = Path(_data_directory.name).resolve()
        _index.INDEX = _index.TOOLBOX_ROOT / 'knowledge/.cache/search.sqlite3'
        _index._INDEX_CACHE.clear()
