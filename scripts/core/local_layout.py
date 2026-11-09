"""The public skeleton of local/; all other local files remain private."""
from pathlib import PurePosixPath

PUBLIC_LOCAL_FILES = {'local/README.md', 'local/knowledge/.gitkeep'}
EMPTY_LOCAL_FILES = {'local/knowledge/.gitkeep'}


def private_local_path(name):
    path = PurePosixPath(name)
    if name.endswith('/') and name.rstrip('/') in {'local', 'local/knowledge'}:
        return False
    return bool(path.parts and path.parts[0] == 'local' and path.as_posix() not in PUBLIC_LOCAL_FILES)


def local_content_issue(name, data):
    if PurePosixPath(name).as_posix() in EMPTY_LOCAL_FILES and data:
        return 'local_placeholder_not_empty'
    return None
