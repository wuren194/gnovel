"""Current-format project creation, shared by the CLI and MCP operations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from .clock import utc_now
from .file_preservation import exclusive_move, observation, parent_directory
from .jsonio import write_bytes, write_json
from .project import current_schema_version, project_template_path, toolbox_manifest, TOOLBOX_ROOT
from .structure_store import StructureError, canonical_json, empty_structure, load_structure, save_structure

CREATE_RECEIPT = '.govel/create-receipt.json'


def checkpoint(stage: str) -> None:
    """Fault injection at durable publication boundaries."""


def payload_hash(value: dict) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def new_project_root(root: Path) -> Path:
    requested = Path(root).expanduser()
    if requested.is_symlink():
        raise StructureError('PROJECT_TARGET_EXISTS', 'target must be a new directory, not a symlink')
    resolved = requested.resolve()
    if resolved.exists():
        raise StructureError('PROJECT_TARGET_EXISTS', 'target directory already exists; nothing was overwritten')
    return resolved


def environment_binding(*, include_template: bool = False) -> dict:
    from .data_provider import binding
    result = {'manifestHash': hashlib.sha256((TOOLBOX_ROOT / 'toolbox.manifest.json').read_bytes()).hexdigest(),
              'provider': binding()}
    if include_template:
        template = project_template_path('agentsMd')
        _, raw = observation(template.resolve())
        if raw is None:
            raise StructureError('PROJECT_TEMPLATE_MISSING', 'project AGENTS template is missing')
        result['templateHash'] = hashlib.sha256(raw).hexdigest()
    return result


def save_lifecycle_plan(root: Path, kind: str, payload: dict) -> tuple[str, str]:
    from .transport_cache import put
    return put(root, kind, payload), payload_hash(payload)


def read_lifecycle_plan(root: Path, kind: str, reference: str, digest: str) -> dict:
    from .transport_cache import get
    plan = get(root, reference, kind=kind)
    if (not isinstance(plan, dict) or payload_hash(plan) != digest
            or plan.get('projectRoot') != str(root)):
        raise StructureError('PLAN_HASH_MISMATCH', 'use the unchanged plan and hash returned by preview')
    return plan


def completed_lifecycle(root: Path, receipt_path: str, reference: str, digest: str) -> dict | None:
    if not root.exists() and not root.is_symlink():
        return None
    try:
        _, raw = observation(root / receipt_path)
        _, marker_raw = observation(root / '.govel/project.json')
        receipt, marker = json.loads(raw or b''), json.loads(marker_raw or b'')
        if receipt.get('planRef') == reference and receipt.get('planHash') == digest:
            if not _valid_lifecycle_receipt(root, receipt_path, receipt, marker):
                raise StructureError('PROJECT_RECEIPT_INVALID', 'stored completion receipt is incomplete or inconsistent')
            return {**receipt['result'], 'alreadyCommitted': True, 'historicalReceipt': True,
                    'currentProjectRevalidated': False}
    except StructureError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    raise StructureError('PROJECT_TARGET_EXISTS', 'target directory is occupied by another project or operation')


def _valid_lifecycle_receipt(root: Path, receipt_path: str, receipt: dict, marker: dict) -> bool:
    """A historical result must still describe the exact approved operation.

    The frozen plan stays in the existing completion receipt, so recovery does
    not depend on a temporary cache or reinterpret later author edits as damage.
    """
    try:
        from .transport_cache import REFERENCE
        creating = receipt_path == CREATE_RECEIPT
        kind = 'project_create' if creating else 'project_restore'
        plan, result = receipt['plan'], receipt['result']
        match = REFERENCE.fullmatch(receipt['planRef'])
        if (set(receipt) != {'schemaVersion', 'kind', 'projectId', 'planRef', 'planHash', 'approved', 'plan', 'result'}
                or type(receipt['schemaVersion']) is not int or receipt['schemaVersion'] != 1
                or receipt['kind'] != kind + '_receipt' or receipt['approved'] is not True
                or not isinstance(plan, dict) or not isinstance(result, dict) or not isinstance(marker, dict)
                or plan['kind'] != kind or payload_hash(plan) != receipt['planHash']
                or match is None or match[1] != kind.replace('_', '-') + '-plan'
                or plan['projectRoot'] != str(root) or not isinstance(plan['projectId'], str) or not plan['projectId']
                or plan['projectId'] != receipt['projectId'] or plan['projectId'] != marker['projectId']
                or result.get('ok') is not True or result.get('alreadyCommitted') is not False):
            return False
        expected = {'ok': True, 'code': 'PROJECT_CREATED' if creating else 'PROJECT_RESTORED',
                    'projectRoot': str(root), 'projectId': plan['projectId'], 'planRef': receipt['planRef'],
                    'planHash': receipt['planHash'], 'alreadyCommitted': False, 'receiptPath': receipt_path,
                    'nextAction': {'operation': 'project.doctor', 'arguments': {'projectRoot': str(root)}}}
        if creating:
            project = result['project']
            if (not isinstance(project, dict) or project.get('schemaVersion') != current_schema_version()
                    or project.get('workflowVersion') != str(toolbox_manifest()['workflowVersion'])
                    or project.get('projectId') != plan['projectId'] or project.get('bookName') != plan['bookName']
                    or project.get('initIdempotencyKey') != receipt['planHash'] or project.get('approvalMode') != 'manual'
                    or type(project.get('projectRevision')) is not int or project['projectRevision'] != 0
                    or not isinstance(project.get('createdAt'), str) or not project['createdAt']):
                return False
            expected.update({'bookName': plan['bookName'], 'project': project})
        else:
            changed = []
            if plan['summary']['sourceRoot'] != str(root) or plan['locatorHistoryAction'] == 'record_snapshot_location':
                changed.append('.govel/project-locator.json')
            if plan['handoffsToStop']:
                changed.append('.govel/handoffs/index.json')
            changed.append(receipt_path)
            preserved = result['preservedMetadata']
            prefix = '.govel/restore-history/' + plan['restoreId'] + '/'
            if (not isinstance(preserved, list) or any(not isinstance(path, str) or not path.startswith(prefix)
                    or path[len(prefix):] not in changed for path in preserved)
                    or len(preserved) != len(set(preserved))
                    or result.get('preservesProjectIdentity') is not True or result.get('hostProcessesRestored') is not False):
                return False
            expected.update({**plan['summary'], 'archive': plan['archive'], 'archiveHash': plan['archiveHash'],
                'preservesProjectIdentity': True, 'hostProcessesRestored': False,
                'stoppedHandoffs': plan['handoffsToStop'], 'locatorHistoryAction': plan['locatorHistoryAction'],
                'changedMetadata': changed, 'preservedMetadata': preserved})
        return result == expected
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def create_staging(root: Path, prefix: str) -> Path:
    # Only a commit creates parents. Previews leave the intended project absent.
    with parent_directory(root, create=True):
        pass
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root.parent))


def sync_tree(root: Path) -> None:
    def unreadable(error):
        raise StructureError('PROJECT_STAGING_UNREADABLE', 'cannot flush every staged project directory',
                             {'path': error.filename}) from error
    for folder, _, files in os.walk(root, topdown=False, onerror=unreadable):
        for name in files:
            fd = os.open(Path(folder) / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def initialize_project(root: Path, *, project_id: str, book_name: str, idempotency_key: str,
                       approval_mode: str) -> tuple[dict, bool, list[str]]:
    """The existing init/quick-init semantics, with one owning implementation.

    The approved creation operation calls this only inside its unpublished tree.
    The one-shot CLI adapter retains its established empty-directory behavior.
    """
    from .data_provider import binding
    from .derived_views import derived_outputs
    from .importer import REQUIRED_PROJECT_DIRS
    from .locks import project_lock_reentrant
    from .prose_binding import initializer_authority
    from .workflow import default_workflow
    from .write_sets import apply_write_set
    root = Path(root).expanduser().resolve()
    if not isinstance(book_name, str) or not book_name.strip():
        raise StructureError('INVALID_BOOK_NAME', 'book name must not be empty')
    if approval_mode not in {'manual', 'auto'}:
        raise StructureError('INVALID_REQUEST', 'approval mode must be manual or auto')
    selected_provider = binding()
    marker_path = root / '.govel/project.json'
    if marker_path.is_file():
        _, raw = observation(marker_path)
        try:
            existing = json.loads(raw or b'')
        except (ValueError, TypeError) as exc:
            raise StructureError('PROJECT_NOT_INITIALIZED', 'corrupt project marker') from exc
        from .project import unsupported_error_code
        if (existing.get('schemaVersion') != current_schema_version()
                or existing.get('workflowVersion') != str(toolbox_manifest()['workflowVersion'])):
            raise StructureError(unsupported_error_code(), 'only the current project format is supported')
        if (existing.get('initIdempotencyKey') == idempotency_key and existing.get('projectId') == project_id
                and existing.get('bookName') == book_name.strip()):
            load_structure(root)
            return existing, False, []
        raise StructureError('PROJECT_ALREADY_INITIALIZED', 'project already exists', {'projectId': existing.get('projectId')})
    if root.exists():
        if not root.is_dir():
            raise StructureError('PROJECT_NOT_EMPTY', 'target must be an empty directory')
        unexpected = sorted(entry.name for entry in root.iterdir() if entry.name not in {'.DS_Store', '.gitkeep'})
        if unexpected:
            raise StructureError('PROJECT_NOT_EMPTY', 'target directory must be empty', {'entries': unexpected})
    for relative in REQUIRED_PROJECT_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    marker = {'schemaVersion': current_schema_version(), 'workflowVersion': str(toolbox_manifest()['workflowVersion']),
              'projectId': project_id, 'bookName': book_name.strip(), 'projectRevision': 0,
              'initIdempotencyKey': idempotency_key, 'createdAt': utc_now(), 'approvalMode': approval_mode}
    if selected_provider['providerId'] is not None:
        marker['dataProvider'] = {'providerId': selected_provider['providerId']}
    write_json(marker_path, marker)
    save_structure(root, empty_structure(project_id, book_name.strip()),
                   _initializer=initializer_authority('project_initialize'))
    with project_lock_reentrant(root):
        write_json(root / '.govel/workflow.json', default_workflow())
        outputs, _ = derived_outputs(root, load_structure(root))
        apply_write_set(root, outputs, operation='initialize_views')
    return marker, True, []


def preview_create_project(root: Path, *, bookName: str, noAgentsMd: bool = False) -> dict:
    root = new_project_root(root)
    if not isinstance(bookName, str) or not bookName.strip():
        raise StructureError('INVALID_BOOK_NAME', 'book name must not be empty')
    if type(noAgentsMd) is not bool:
        raise StructureError('INVALID_REQUEST', 'noAgentsMd must be boolean')
    plan = {'kind': 'project_create', 'projectRoot': str(root), 'projectId': str(uuid.uuid4()),
            'bookName': bookName.strip(), 'noAgentsMd': noAgentsMd, 'createdAt': utc_now(),
            'environment': environment_binding(include_template=not noAgentsMd)}
    reference, digest = save_lifecycle_plan(root, 'project-create-plan', plan)
    return {'ok': True, 'code': 'PROJECT_CREATE_PREVIEW', 'projectRoot': str(root),
            'bookName': plan['bookName'], 'projectId': plan['projectId'], 'planRef': reference, 'planHash': digest,
            'chapterCount': 0, 'approvalMode': 'manual', 'copiesAgentsTemplate': not noAgentsMd,
            'requiresApproval': True, 'commitAction': {'operation': 'project.create-commit',
                'arguments': {'projectRoot': str(root), 'planRef': reference, 'planHash': digest, 'approve': True}}}


def commit_create_project(root: Path, *, planRef: str, planHash: str, approve: bool = False) -> dict:
    if approve is not True:
        raise StructureError('APPROVAL_REQUIRED', 'explicit approval of the creation preview is required')
    root = Path(root).expanduser().resolve()
    completed = completed_lifecycle(root, CREATE_RECEIPT, planRef, planHash)
    if completed is not None:
        return completed
    plan = read_lifecycle_plan(root, 'project-create-plan', planRef, planHash)
    if plan['environment'] != environment_binding(include_template=not plan['noAgentsMd']):
        raise StructureError('PROJECT_CREATE_PLAN_STALE', 'engine, provider or template changed; preview again')
    staging = create_staging(root, '.govel-create-')
    try:
        marker, _, _ = initialize_project(staging, project_id=plan['projectId'], book_name=plan['bookName'],
                                           idempotency_key=planHash, approval_mode='manual')
        if not plan['noAgentsMd']:
            _, template = observation(project_template_path('agentsMd').resolve())
            if template is None or hashlib.sha256(template).hexdigest() != plan['environment']['templateHash']:
                raise StructureError('PROJECT_CREATE_PLAN_STALE', 'project template changed after preview')
            write_bytes(staging / 'AGENTS.md', template)
        result = {'ok': True, 'code': 'PROJECT_CREATED', 'projectRoot': str(root), 'projectId': plan['projectId'],
                  'bookName': plan['bookName'], 'project': marker, 'planRef': planRef, 'planHash': planHash,
                  'alreadyCommitted': False, 'receiptPath': CREATE_RECEIPT,
                  'nextAction': {'operation': 'project.doctor', 'arguments': {'projectRoot': str(root)}}}
        write_json(staging / CREATE_RECEIPT, {'schemaVersion': 1, 'kind': 'project_create_receipt', 'projectId': plan['projectId'],
            'planRef': planRef, 'planHash': planHash, 'approved': True, 'plan': plan, 'result': result})
        from .project_archive_io import inventory
        frozen = inventory(staging)
        sync_tree(staging)
        if inventory(staging) != frozen:
            raise StructureError('PROJECT_CREATE_STAGING_CHANGED', 'new project files changed while flushing')
        checkpoint('create-before-publication')
        if plan['environment'] != environment_binding(include_template=not plan['noAgentsMd']):
            raise StructureError('PROJECT_CREATE_PLAN_STALE', 'creation inputs changed during staging; preview again')
        if inventory(staging) != frozen:
            raise StructureError('PROJECT_CREATE_STAGING_CHANGED', 'new project files changed before publication')
        try:
            exclusive_move(staging, root)
        except FileExistsError:
            return completed_lifecycle(root, CREATE_RECEIPT, planRef, planHash)
        checkpoint('create-after-publication')
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)
