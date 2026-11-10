"""Streaming inventories and validation for the current project backup format."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import unicodedata
import zipfile

from .file_preservation import parent_directory
from .project import current_schema_version, toolbox_manifest, workflow_policy
from .structure_store import StructureError, validate_structure

MANIFEST_NAME = 'manifest.json'
BACKUP_KIND = 'govel_project_backup'
CHUNK_SIZE = 1 << 20


def archive_limits() -> dict:
    policy = workflow_policy()['maintenance']
    keys = {'files': 'projectArchiveMaxFiles', 'bytes': 'projectArchiveMaxBytes',
            'metadata': 'projectArchiveMaxMetadataBytes'}
    result = {key: policy.get(field) for key, field in keys.items()}
    if any(type(value) is not int or value < 1 for value in result.values()):
        raise ValueError('maintenance project archive limits must be positive integers')
    return result


def file_signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def regular_file(path: Path):
    with parent_directory(path) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'backup input must be a regular file', {'path': str(path)})
            yield stream, before
            after = os.fstat(stream.fileno())
            try:
                current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise StructureError('PROJECT_ARCHIVE_INPUT_CHANGED', 'input disappeared during reading', {'path': str(path)}) from exc
            if file_signature(before) != file_signature(after) or file_signature(after) != file_signature(current):
                raise StructureError('PROJECT_ARCHIVE_INPUT_CHANGED', 'input changed during reading', {'path': str(path)})


def stream_digest(stream) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: str) -> str:
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or value.startswith('/') or any(part in {'', '.', '..'} for part in value.split('/'))
            or PurePosixPath(value).as_posix() != value):
        raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'archive contains an unsafe project-relative path')
    return value


def inventory(root: Path) -> dict[str, dict]:
    limits = archive_limits()
    rows, names, total = {}, set(), 0
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'project root must be a directory')
    rows[''] = {'kind': 'directory', 'mode': stat.S_IMODE(root_info.st_mode), 'signature': file_signature(root_info)}
    def unreadable(error):
        raise StructureError('PROJECT_ARCHIVE_UNREADABLE', 'cannot enumerate every project directory',
                             {'path': error.filename}) from error

    for folder, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in sorted([*directories, *files]):
            path = Path(folder) / name
            relative = safe_relative(path.relative_to(root).as_posix())
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'symlinks and special files cannot be backed up', {'path': relative})
            key = unicodedata.normalize('NFC', relative).casefold()
            if key in names:
                raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'project contains colliding portable paths', {'path': relative})
            names.add(key)
            kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
            rows[relative] = {'kind': kind, 'mode': stat.S_IMODE(info.st_mode), 'signature': file_signature(info)}
            if kind == 'file':
                rows[relative]['bytes'] = info.st_size
                total += info.st_size
            if len(rows) - 1 > limits['files'] or total > limits['bytes']:
                raise StructureError('PROJECT_ARCHIVE_LIMIT', 'project exceeds the configured backup limits', limits)
    return rows


def validate_project_documents(marker: dict, source: dict) -> None:
    from .project import unsupported_error_code
    if (not isinstance(marker, dict) or marker.get('schemaVersion') != current_schema_version()
            or marker.get('workflowVersion') != str(toolbox_manifest()['workflowVersion'])):
        raise StructureError(unsupported_error_code(), 'backup supports only the current project format')
    validate_structure(source)
    if marker.get('projectId') != source.get('projectId'):
        raise StructureError('PROJECT_ARCHIVE_INVALID', 'project marker and structure identities differ')


def validate_manifest(manifest: dict, members: list[zipfile.ZipInfo]) -> dict[str, dict]:
    limits = archive_limits()
    if (not isinstance(manifest, dict) or manifest.get('kind') != BACKUP_KIND or manifest.get('schemaVersion') != 1
            or manifest.get('projectSchemaVersion') != current_schema_version()
            or manifest.get('workflowVersion') != str(toolbox_manifest()['workflowVersion'])
            or not isinstance(manifest.get('entries'), list) or type(manifest.get('rootMode')) is not int
            or not 0 <= manifest['rootMode'] <= 0o7777):
        raise StructureError('PROJECT_ARCHIVE_INVALID', 'archive is not a supported current project backup')
    origin = manifest.get('sourceRoot')
    if not isinstance(origin, str) or not Path(origin).is_absolute() or '..' in Path(origin).parts:
        raise StructureError('PROJECT_ARCHIVE_INVALID', 'archive does not name a safe original project root')
    if len(manifest['entries']) > limits['files'] or len(members) != len(manifest['entries']) + 1:
        raise StructureError('PROJECT_ARCHIVE_INVALID', 'archive member list does not match its manifest')
    rows, names, total = {}, set(), 0
    for entry in manifest['entries']:
        if not isinstance(entry, dict):
            raise StructureError('PROJECT_ARCHIVE_INVALID', 'invalid manifest entry')
        relative = safe_relative(entry.get('path'))
        key = unicodedata.normalize('NFC', relative).casefold()
        if key in names or entry.get('kind') not in {'file', 'directory'}:
            raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'archive contains duplicate or colliding paths')
        names.add(key)
        if type(entry.get('mode')) is not int or not 0 <= entry['mode'] <= 0o7777:
            raise StructureError('PROJECT_ARCHIVE_INVALID', 'invalid file mode')
        if entry['kind'] == 'file':
            if (type(entry.get('bytes')) is not int or entry['bytes'] < 0
                    or not isinstance(entry.get('sha256'), str) or len(entry['sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in entry['sha256'])):
                raise StructureError('PROJECT_ARCHIVE_INVALID', 'invalid file size or digest')
            total += entry['bytes']
        rows[relative] = entry
    if total > limits['bytes']:
        raise StructureError('PROJECT_ARCHIVE_LIMIT', 'archive exceeds the configured expanded size limit', limits)
    for relative in rows:
        for parent in PurePosixPath(relative).parents:
            if str(parent) != '.' and rows.get(str(parent), {}).get('kind') != 'directory':
                raise StructureError('PROJECT_ARCHIVE_INVALID', 'archive omits a parent directory')
    expected = {MANIFEST_NAME}
    for relative, entry in rows.items():
        expected.add('project/' + relative + ('/' if entry['kind'] == 'directory' else ''))
    actual = [info.filename for info in members]
    if len(set(actual)) != len(actual) or set(actual) != expected:
        raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'archive has undeclared or repeated members')
    for info in members:
        if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise StructureError('PROJECT_ARCHIVE_INVALID', 'encrypted or unsupported archive member')
        if info.filename == MANIFEST_NAME:
            continue
        relative = info.filename[len('project/'):].rstrip('/')
        entry = rows[relative]
        expected_type = stat.S_IFDIR if entry['kind'] == 'directory' else stat.S_IFREG
        stored_type = stat.S_IFMT(info.external_attr >> 16)
        if stored_type not in {0, expected_type} or info.file_size != entry.get('bytes', 0):
            raise StructureError('PROJECT_ARCHIVE_INVALID', 'member type or size differs from the manifest')
    for relative in ('.govel/project.json', '.govel/structure.json', '.govel/workflow.json'):
        if rows.get(relative, {}).get('kind') != 'file':
            raise StructureError('PROJECT_ARCHIVE_INVALID', 'archive lacks a required project file', {'path': relative})
    for relative in ('.govel/project-locator.json', '.govel/restore-receipt.json', '.govel/handoffs/index.json'):
        if relative in rows and rows[relative]['kind'] != 'file':
            raise StructureError('PROJECT_ARCHIVE_INVALID', 'project metadata path is not a file', {'path': relative})
    if '.govel/restore-history' in rows and rows['.govel/restore-history']['kind'] != 'directory':
        raise StructureError('PROJECT_ARCHIVE_INVALID', 'restore-history must be a directory')
    if any(relative.startswith('.govel/transactions/') and relative.count('/') == 2 and relative.endswith('.json') for relative in rows):
        raise StructureError('TRANSACTION_INCOMPLETE', 'archive contains an unfinished transaction; recover the source project before backing up')
    return rows


def read_metadata(archive: zipfile.ZipFile, name: str):
    info = archive.getinfo(name)
    if info.file_size > archive_limits()['metadata']:
        raise StructureError('PROJECT_ARCHIVE_LIMIT', 'archive JSON metadata exceeds the configured limit')
    try:
        return json.loads(archive.read(info))
    except (UnicodeDecodeError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise StructureError('PROJECT_ARCHIVE_INVALID', 'archive metadata is corrupt') from exc


@contextmanager
def verified_archive(path: Path, expected_hash: str | None = None):
    """Validate the entire package while keeping the same source descriptor open."""
    with regular_file(path) as (stream, info):
        limits = archive_limits()
        if info.st_size > limits['bytes'] + limits['metadata']:
            raise StructureError('PROJECT_ARCHIVE_LIMIT', 'backup file exceeds the configured archive size limit')
        digest = stream_digest(stream)
        if expected_hash is not None and expected_hash != digest:
            raise StructureError('PROJECT_ARCHIVE_HASH_MISMATCH', 'backup changed after preview or differs from the expected hash')
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as archive:
                members = archive.infolist()
                if len(members) > archive_limits()['files'] + 1:
                    raise StructureError('PROJECT_ARCHIVE_LIMIT', 'archive exceeds the configured member limit')
                manifest = read_metadata(archive, MANIFEST_NAME)
                rows = validate_manifest(manifest, members)
                for relative, entry in rows.items():
                    if entry['kind'] == 'file':
                        with archive.open('project/' + relative) as member:
                            if stream_digest(member) != entry['sha256']:
                                raise StructureError('PROJECT_ARCHIVE_HASH_MISMATCH', 'backup member failed digest verification', {'path': relative})
                marker = read_metadata(archive, 'project/.govel/project.json')
                source = read_metadata(archive, 'project/.govel/structure.json')
                validate_project_documents(marker, source)
                if (manifest.get('projectId') != source['projectId'] or manifest.get('revision') != source['revision']
                        or manifest.get('bookName') != source['bookName']):
                    raise StructureError('PROJECT_ARCHIVE_INVALID', 'manifest and project identity disagree')
                yield archive, manifest, digest, info.st_size
        except (KeyError, UnicodeDecodeError, zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError) as exc:
            raise StructureError('PROJECT_ARCHIVE_INVALID', 'backup archive is incomplete or corrupt') from exc
