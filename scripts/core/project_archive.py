"""Complete local project backups and approved restoration into a new directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
import zipfile

from .clock import utc_now
from .file_preservation import exclusive_move, parent_directory
from .jsonio import write_bytes, write_json
from .locks import project_lock_reentrant
from .project import current_schema_version, toolbox_manifest, TOOLBOX_ROOT
from .project_archive_io import (BACKUP_KIND, CHUNK_SIZE, MANIFEST_NAME, archive_limits, file_signature,
                                inventory, read_metadata, regular_file, validate_manifest, validate_project_documents,
                                verified_archive)
from .project_lifecycle import (completed_lifecycle, create_staging, new_project_root, payload_hash,
                               read_lifecycle_plan, save_lifecycle_plan, sync_tree)
from .structure_store import StructureError, load_structure

RESTORE_RECEIPT = '.govel/restore-receipt.json'


def checkpoint(stage: str) -> None:
    """Fault injection around snapshot validation and exclusive publication."""


def _absolute_file(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'an explicit absolute file path is required')
    requested = Path(value).expanduser()
    if not requested.is_absolute() or requested.is_symlink():
        raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'use an absolute file path without a symlink target')
    return requested.resolve()


def _zip_info(relative: str, entry: dict) -> zipfile.ZipInfo:
    directory = entry['kind'] == 'directory'
    info = zipfile.ZipInfo('project/' + relative + ('/' if directory else ''))
    info.create_system = 3
    info.external_attr = ((stat.S_IFDIR if directory else stat.S_IFREG) | entry['mode']) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def backup_project(root: Path, *, output: str, expectedRevision: int | None = None) -> dict:
    root = Path(root).expanduser().resolve()
    target = _absolute_file(output)
    if target == root or root in target.parents:
        raise StructureError('PROJECT_ARCHIVE_PATH_INVALID', 'backup output must be outside the project')
    if target.exists():
        raise StructureError('PROJECT_BACKUP_EXISTS', 'backup output already exists; choose a new file')
    with project_lock_reentrant(root):
        source = load_structure(root)
        if expectedRevision is not None and (type(expectedRevision) is not int or expectedRevision != source['revision']):
            raise StructureError('REVISION_CONFLICT', 'project revision differs from the requested snapshot')
        before = inventory(root)
        with parent_directory(target, create=True):
            pass
        fd, name = tempfile.mkstemp(prefix='.govel-backup-', dir=target.parent)
        temporary = Path(name)
        os.close(fd)
        try:
            entries, documents = [], {}
            with zipfile.ZipFile(temporary, 'w', allowZip64=True) as archive:
                for relative, entry in sorted(before.items()):
                    if not relative:
                        continue
                    row = {'path': relative, 'kind': entry['kind'], 'mode': entry['mode']}
                    info = _zip_info(relative, entry)
                    if entry['kind'] == 'directory':
                        archive.writestr(info, b'')
                    else:
                        digest, size, metadata = hashlib.sha256(), 0, bytearray()
                        is_document = relative in {'.govel/project.json', '.govel/structure.json'}
                        if is_document and entry['bytes'] > archive_limits()['metadata']:
                            raise StructureError('PROJECT_ARCHIVE_LIMIT', 'project metadata exceeds configured archive limit')
                        with regular_file(root / relative) as (stream, state), archive.open(info, 'w', force_zip64=True) as member:
                            if file_signature(state) != entry['signature']:
                                raise StructureError('PROJECT_ARCHIVE_INPUT_CHANGED', 'project changed during backup', {'path': relative})
                            while chunk := stream.read(CHUNK_SIZE):
                                if size + len(chunk) > entry['bytes']:
                                    raise StructureError('PROJECT_ARCHIVE_INPUT_CHANGED', 'file grew during backup', {'path': relative})
                                member.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                                if is_document:
                                    metadata.extend(chunk)
                        row.update({'sha256': digest.hexdigest(), 'bytes': size})
                        if is_document:
                            try:
                                documents[relative] = json.loads(metadata)
                            except (ValueError, UnicodeDecodeError) as exc:
                                raise StructureError('PROJECT_ARCHIVE_INPUT_CHANGED', 'project metadata changed during backup') from exc
                    entries.append(row)
                validate_project_documents(documents['.govel/project.json'], documents['.govel/structure.json'])
                manifest = {'schemaVersion': 1, 'kind': BACKUP_KIND, 'projectSchemaVersion': current_schema_version(),
                            'workflowVersion': str(toolbox_manifest()['workflowVersion']), 'sourceRoot': str(root),
                            'projectId': source['projectId'], 'bookName': source['bookName'], 'revision': source['revision'],
                            'createdAt': utc_now(), 'rootMode': before['']['mode'], 'entries': entries}
                payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
                if len(payload) > archive_limits()['metadata']:
                    raise StructureError('PROJECT_ARCHIVE_LIMIT', 'backup manifest exceeds configured metadata limit')
                archive.writestr(MANIFEST_NAME, payload, compress_type=zipfile.ZIP_DEFLATED)
                validate_manifest(manifest, archive.infolist())
            checkpoint('backup-captured')
            if inventory(root) != before:
                raise StructureError('PROJECT_ARCHIVE_INPUT_CHANGED', 'project changed during backup; retry from the current state')
            with regular_file(temporary) as (stream, _):
                from .project_archive_io import stream_digest
                archive_hash = stream_digest(stream)
                os.fsync(stream.fileno())
            checkpoint('backup-before-publication')
            if inventory(root) != before:
                raise StructureError('PROJECT_ARCHIVE_INPUT_CHANGED', 'project changed before backup publication')
            try:
                exclusive_move(temporary, target)
            except FileExistsError as exc:
                raise StructureError('PROJECT_BACKUP_EXISTS', 'backup output became occupied; nothing was overwritten') from exc
            return {'ok': True, 'code': 'PROJECT_BACKUP_CREATED', 'output': str(target), 'archiveHash': archive_hash,
                    **_summary(manifest), 'snapshotComplete': True, 'hostProcessesIncluded': False,
                    'nextAction': {'operation': 'project.backup-verify', 'arguments': {'archive': str(target),
                                                                                     'expectedArchiveHash': archive_hash}}}
        finally:
            if temporary.exists():
                temporary.unlink()


def _summary(manifest: dict) -> dict:
    entries = manifest['entries']
    return {'projectId': manifest['projectId'], 'bookName': manifest['bookName'], 'revision': manifest['revision'],
            'sourceRoot': manifest['sourceRoot'], 'createdAt': manifest['createdAt'],
            'fileCount': sum(row['kind'] == 'file' for row in entries),
            'directoryCount': sum(row['kind'] == 'directory' for row in entries),
            'projectBytes': sum(row.get('bytes', 0) for row in entries)}


def verify_project_backup(*, archive: str, expectedArchiveHash: str | None = None) -> dict:
    path = _absolute_file(archive)
    with verified_archive(path, expectedArchiveHash) as (_, manifest, digest, size):
        result = {'ok': True, 'code': 'PROJECT_BACKUP_VERIFIED', 'archive': str(path), 'archiveHash': digest,
                  'archiveBytes': size, 'snapshotComplete': True, **_summary(manifest)}
    return result


def _open_handoffs(archive: zipfile.ZipFile) -> list[str]:
    name = 'project/.govel/handoffs/index.json'
    if name not in archive.namelist():
        return []
    from .handoff_records import HANDOFF_INDEX_SCHEMA_VERSION, OPEN_STATUSES
    index = read_metadata(archive, name)
    if (not isinstance(index, dict) or index.get('schemaVersion') != HANDOFF_INDEX_SCHEMA_VERSION
            or not isinstance(index.get('handoffs'), dict)):
        raise StructureError('PROJECT_ARCHIVE_INVALID', 'backup handoff index is invalid')
    return sorted(key for key, row in index['handoffs'].items()
                  if isinstance(row, dict) and row.get('status') in OPEN_STATUSES)


def _locator_history_action(archive: zipfile.ZipFile, manifest: dict) -> str:
    name = 'project/.govel/project-locator.json'
    if name not in archive.namelist():
        return 'create'
    receipt = read_metadata(archive, name)
    if (not isinstance(receipt, dict) or receipt.get('schemaVersion') != 1 or receipt.get('kind') != 'project_locator'
            or receipt.get('projectId') != manifest['projectId'] or not isinstance(receipt.get('history'), list)
            or not receipt['history']):
        raise StructureError('PROJECT_ARCHIVE_LOCATOR_INVALID', 'repair the invalid source locator history before restoring')
    previous = None
    for row in receipt['history']:
        if not isinstance(row, dict):
            raise StructureError('PROJECT_ARCHIVE_LOCATOR_INVALID', 'source locator history is invalid')
        origin, target = row.get('rootAtOperation'), row.get('currentLocator')
        if (any(not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts
                for value in (origin, target)) or not row.get('operationId')
                or previous is not None and origin != previous):
            raise StructureError('PROJECT_ARCHIVE_LOCATOR_INVALID', 'source locator history has an unverified gap')
        previous = target
    if (previous != receipt.get('currentLocator')
            or receipt.get('rootAtOperation') != receipt['history'][-1]['rootAtOperation']):
        raise StructureError('PROJECT_ARCHIVE_LOCATOR_INVALID', 'source locator receipt disagrees with its history')
    return 'append' if previous == manifest['sourceRoot'] else 'record_snapshot_location'


def preview_restore_project(root: Path, *, archive: str, expectedArchiveHash: str | None = None) -> dict:
    root = new_project_root(root)
    path = _absolute_file(archive)
    with verified_archive(path, expectedArchiveHash) as (opened, manifest, digest, _):
        handoffs = _open_handoffs(opened)
        plan = {'kind': 'project_restore', 'projectRoot': str(root), 'archive': str(path), 'archiveHash': digest,
                'manifestHash': payload_hash(manifest), 'projectId': manifest['projectId'],
                'restoreId': uuid.uuid4().hex, 'handoffsToStop': handoffs,
                'locatorHistoryAction': _locator_history_action(opened, manifest),
                'engineManifestHash': hashlib.sha256((TOOLBOX_ROOT / 'toolbox.manifest.json').read_bytes()).hexdigest(),
                'summary': _summary(manifest)}
    reference, digest = save_lifecycle_plan(root, 'project-restore-plan', plan)
    return {'ok': True, 'code': 'PROJECT_RESTORE_PREVIEW', 'projectRoot': str(root), **plan['summary'],
            'archive': str(path), 'archiveHash': plan['archiveHash'], 'planRef': reference, 'planHash': digest,
            'preservesProjectIdentity': True, 'hostProcessesRestored': False, 'handoffsToStop': handoffs,
            'locatorHistoryAction': plan['locatorHistoryAction'],
            'requiresApproval': True, 'commitAction': {'operation': 'project.restore-commit',
                'arguments': {'projectRoot': str(root), 'planRef': reference, 'planHash': digest, 'approve': True}}}


def _restore_metadata(staging: Path, root: Path, manifest: dict, plan: dict, plan_hash: str) -> tuple[list[str], list[str]]:
    from .project_relocate import RECEIPT_PATH, locator_receipt_bytes
    changed, preserved = [], []

    def preserve(relative):
        path = staging / relative
        if path.is_file():
            destination = '.govel/restore-history/' + plan['restoreId'] + '/' + relative
            write_bytes(staging / destination, path.read_bytes())
            preserved.append(destination)

    preserve(RESTORE_RECEIPT)
    if manifest['sourceRoot'] != str(root) or plan['locatorHistoryAction'] == 'record_snapshot_location':
        preserve(RECEIPT_PATH)
        # The original receipt remains in restore-history. The owning locator
        # helper appends the new location without changing authored evidence.
        if plan['locatorHistoryAction'] == 'record_snapshot_location':
            existing = json.loads((staging / RECEIPT_PATH).read_bytes())
            bridge = locator_receipt_bytes(staging, root_at_operation=existing['currentLocator'],
                current_locator=manifest['sourceRoot'], project_id=manifest['projectId'], actor='author',
                operation_id='RESTORE-SNAPSHOT-' + plan['restoreId'], relocated_at=utc_now(),
                plan_hash=plan_hash, inventory_hash=plan['manifestHash'], file_count=plan['summary']['fileCount'])
            write_bytes(staging / RECEIPT_PATH, bridge)
        if manifest['sourceRoot'] != str(root):
            receipt = locator_receipt_bytes(staging, root_at_operation=manifest['sourceRoot'], current_locator=str(root),
                project_id=manifest['projectId'], actor='author', operation_id='RESTORE-' + plan['restoreId'],
                relocated_at=utc_now(), plan_hash=plan_hash, inventory_hash=plan['manifestHash'],
                file_count=plan['summary']['fileCount'])
            write_bytes(staging / RECEIPT_PATH, receipt)
        changed.append(RECEIPT_PATH)
    if plan['handoffsToStop']:
        from .handoff_records import HANDOFFS_PATH, stop_handoff
        preserve(HANDOFFS_PATH)
        for handoff in plan['handoffsToStop']:
            stop_handoff(staging, handoff, reason='作者批准恢复项目；原宿主任务不在恢复副本中继续运行。')
        changed.append(HANDOFFS_PATH)
    return changed, preserved


def _validate_restored(staging: Path, manifest: dict, changed: list[str], preserved: list[str], restore_id: str) -> dict:
    """Check both original bytes and the exact set of approved metadata additions."""
    from .project_archive_io import stream_digest
    expected = {entry['path']: entry for entry in manifest['entries']}
    allowed = set(expected) | set(changed) | {RESTORE_RECEIPT} | set(preserved)
    for relative in list(allowed):
        allowed.update(str(parent) for parent in Path(relative).parents if str(parent) != '.')
    current = inventory(staging)
    if set(current) - {''} != allowed:
        raise StructureError('PROJECT_RESTORE_STAGING_CHANGED', 'restored tree differs from the approved file set')
    originals = {relative: entry for relative, entry in expected.items() if relative not in changed and relative != RESTORE_RECEIPT}
    prefix = '.govel/restore-history/' + restore_id + '/'
    originals.update({relative: expected[relative[len(prefix):]] for relative in preserved})
    for relative, entry in originals.items():
        if current[relative]['kind'] != entry['kind']:
            raise StructureError('PROJECT_RESTORE_STAGING_CHANGED', 'restored member changed type', {'path': relative})
        if entry['kind'] == 'file':
            if relative in expected and current[relative]['mode'] != entry['mode']:
                raise StructureError('PROJECT_RESTORE_STAGING_CHANGED', 'restored file mode changed', {'path': relative})
            with regular_file(staging / relative) as (stream, _):
                if stream_digest(stream) != entry['sha256']:
                    raise StructureError('PROJECT_RESTORE_STAGING_CHANGED', 'restored original bytes changed', {'path': relative})
    return current


def _discard_staging(staging: Path) -> None:
    # Imported directories may legitimately have read-only modes. They are still
    # our unpublished staging tree, so restore write access before removing it.
    if staging.exists():
        os.chmod(staging, 0o700)
        for folder, directories, _ in os.walk(staging, followlinks=False):
            for name in directories:
                path = Path(folder) / name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
        shutil.rmtree(staging)


def commit_restore_project(root: Path, *, planRef: str, planHash: str, approve: bool = False) -> dict:
    if approve is not True:
        raise StructureError('APPROVAL_REQUIRED', 'explicit approval of the restore preview is required')
    root = Path(root).expanduser().resolve()
    completed = completed_lifecycle(root, RESTORE_RECEIPT, planRef, planHash)
    if completed is not None:
        return completed
    plan = read_lifecycle_plan(root, 'project-restore-plan', planRef, planHash)
    if plan['engineManifestHash'] != hashlib.sha256((TOOLBOX_ROOT / 'toolbox.manifest.json').read_bytes()).hexdigest():
        raise StructureError('PROJECT_RESTORE_PLAN_STALE', 'engine changed after restore preview; preview again')
    staging = create_staging(root, '.govel-restore-')
    try:
        with verified_archive(Path(plan['archive']), plan['archiveHash']) as (archive, manifest, _, _):
            if payload_hash(manifest) != plan['manifestHash'] or _open_handoffs(archive) != plan['handoffsToStop']:
                raise StructureError('PLAN_HASH_MISMATCH', 'backup no longer matches the restore preview')
            for entry in manifest['entries']:
                path = staging / entry['path']
                if entry['kind'] == 'directory':
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open('project/' + entry['path']) as source, path.open('xb') as target:
                    digest = hashlib.sha256()
                    while chunk := source.read(CHUNK_SIZE):
                        target.write(chunk)
                        digest.update(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if digest.hexdigest() != entry['sha256']:
                    raise StructureError('PROJECT_ARCHIVE_HASH_MISMATCH', 'backup changed during extraction')
                os.chmod(path, entry['mode'])
            checkpoint('restore-extracted')
            changed, preserved = _restore_metadata(staging, root, manifest, plan, planHash)
            result = {'ok': True, 'code': 'PROJECT_RESTORED', 'projectRoot': str(root), **plan['summary'],
                      'archive': plan['archive'], 'archiveHash': plan['archiveHash'], 'planRef': planRef, 'planHash': planHash,
                      'preservesProjectIdentity': True, 'alreadyCommitted': False, 'receiptPath': RESTORE_RECEIPT,
                      'hostProcessesRestored': False, 'stoppedHandoffs': plan['handoffsToStop'],
                      'locatorHistoryAction': plan['locatorHistoryAction'],
                      'changedMetadata': [*changed, RESTORE_RECEIPT], 'preservedMetadata': preserved,
                      'nextAction': {'operation': 'project.doctor', 'arguments': {'projectRoot': str(root)}}}
            write_json(staging / RESTORE_RECEIPT, {'schemaVersion': 1, 'kind': 'project_restore_receipt', 'projectId': manifest['projectId'],
                'planRef': planRef, 'planHash': planHash, 'approved': True, 'plan': plan, 'result': result})
            _validate_restored(staging, manifest, changed, preserved, plan['restoreId'])
            sync_tree(staging)
            for entry in reversed(manifest['entries']):
                if entry['kind'] == 'directory':
                    os.chmod(staging / entry['path'], entry['mode'])
            os.chmod(staging, manifest['rootMode'])
            before_publication = inventory(staging)
            checkpoint('restore-before-publication')
            if inventory(staging) != before_publication:
                raise StructureError('PROJECT_RESTORE_STAGING_CHANGED', 'restored files changed before publication')
        # Leaving verified_archive checks that the source archive inode and
        # bytes did not change during extraction, before the publication point.
        try:
            exclusive_move(staging, root)
        except FileExistsError:
            return completed_lifecycle(root, RESTORE_RECEIPT, planRef, planHash)
        checkpoint('restore-after-publication')
        return result
    finally:
        _discard_staging(staging)
