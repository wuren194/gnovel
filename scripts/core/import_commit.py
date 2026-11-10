"""Approved greenfield import: publish a complete project with a single exclusive rename."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from .clock import utc_now
from .file_preservation import exclusive_move, observation
from .importer import REQUIRED_PROJECT_DIRS
from .jsonio import write_bytes, write_json
from .manuscript_import import decode_source, digest, read_plan, source_files
from .project import TOOLBOX_ROOT, current_schema_version, toolbox_manifest
from .structure_store import StructureError, empty_structure, new_id, validate_structure

RECEIPT = '.govel/import-receipt.json'


def checkpoint(stage: str) -> None:
    """Fault injection for staging and publication, never a runtime decision."""


def _completed(root: Path, plan_id: str, plan_hash: str) -> dict | None:
    if not root.exists() and not root.is_symlink():
        return None
    try:
        _, raw = observation(root / RECEIPT)
        receipt = json.loads(raw or b'')
        _, marker = observation(root / '.govel/project.json')
        if (receipt['planId'] == plan_id and receipt['planHash'] == plan_hash
                and receipt['projectId'] == json.loads(marker or b'')['projectId']
                and receipt['result']['projectRoot'] == str(root)):
            return {**receipt['result'], 'alreadyCommitted': True, 'historicalReceipt': True,
                    'currentProjectRevalidated': False}
    except (OSError, ValueError, KeyError, TypeError, StructureError):
        pass
    raise StructureError('IMPORT_TARGET_EXISTS', 'destination is occupied; nothing was overwritten')


def _verify_sources(plan: dict) -> None:
    paths = source_files(Path(plan['source']))
    if [str(path) for path in paths] != [row['absolutePath'] for row in plan['files']]:
        raise StructureError('IMPORT_SOURCE_CHANGED', 'source file set changed; preview again')
    for row in plan['files']:
        state, _ = observation(Path(row['absolutePath']))
        if state != row['sourceObject']:
            raise StructureError('IMPORT_SOURCE_CHANGED', 'source changed after preview; preview again', {'sourcePath': row['sourcePath']})


def _build(staging: Path, directory: Path, plan: dict) -> dict:
    from .data_provider import binding
    from .prose_binding import require_all_binding_health, seal_binding
    from .workflow import default_workflow
    structure = empty_structure(plan['projectId'], plan['bookName'])
    structure['revision'] = 1
    structure['volumes'][0]['title'] = '导入卷（待整理）'
    structure['arcs'][0]['title'] = '待整理主线'
    for relative in REQUIRED_PROJECT_DIRS:
        (staging / relative).mkdir(parents=True, exist_ok=True)
    texts = {}
    for row in plan['files']:
        _, raw = observation(directory / row['snapshotPath'])
        if raw is None or digest(raw) != row['sourceHash']:
            raise StructureError('IMPORT_SNAPSHOT_DAMAGED', 'frozen manuscript evidence changed')
        texts[row['sourcePath']] = decode_source(raw, plan['encoding']).encode('utf-8')
        write_bytes(staging / '.govel/import-source' / row['snapshotPath'], raw)
    mappings = []
    for ordinal, section in enumerate(plan['chapters'], 1):
        raw = texts[section['sourcePath']][section['utf8ByteStart']:section['utf8ByteEnd']]
        if digest(raw) != section['contentHash']:
            raise StructureError('IMPORT_SNAPSHOT_DAMAGED', 'frozen chapter content changed')
        cid = new_id('chapter')
        relative = f'正文/{cid}.md'
        chapter = {'chapterId': cid, 'displayOrdinal': ordinal, 'displayCode': f'CH{ordinal:03d}',
                   'title': section['title'], 'placement': {'scope': 'volume', 'volumeId': structure['volumes'][0]['volumeId']},
                   'filePath': relative, 'status': 'planning_required'}
        # This named initializer creates NEW bytes in a private, empty staging tree.
        # It never claims an existing project file or inherits foreign approval.
        chapter['proseBinding'] = seal_binding(structure['projectId'], cid, relative, state='managed',
                                               generation=1, expected_content_digest='sha256:' + digest(raw))
        write_bytes(staging / relative, raw)
        structure['chapters'].append(chapter)
        structure['chapterArcLinks'].append({'chapterId': cid, 'arcId': structure['arcs'][0]['arcId'], 'role': 'primary'})
        mappings.append({**section, 'chapterId': cid, 'filePath': relative})
    structure['migrations'].append({'kind': 'manuscript_import', 'planId': plan['planId'], 'planHash': plan['planHash'], 'createdAt': utc_now()})
    validate_structure(structure)
    require_all_binding_health(structure)
    marker = {'schemaVersion': current_schema_version(), 'workflowVersion': str(toolbox_manifest()['workflowVersion']),
              'projectId': plan['projectId'], 'projectRevision': 0, 'bookName': plan['bookName'],
              'createdAt': utc_now(), 'approvalMode': 'manual'}
    provider = binding()
    if provider['providerId'] is not None:
        marker['dataProvider'] = {'providerId': provider['providerId']}
    write_json(staging / '.govel/project.json', marker)
    write_json(staging / '.govel/structure.json', structure)
    write_json(staging / '.govel/workflow.json', default_workflow())
    template = TOOLBOX_ROOT / toolbox_manifest()['sharedProtocols']['projectTemplates']['agentsMd']
    write_bytes(staging / 'AGENTS.md', template.read_bytes())
    import importlib
    state_cache = importlib.import_module('scripts.state_cache' if __package__.startswith('scripts.') else 'state_cache')
    state_cache.rebuild(staging)
    result = {'ok': True, 'code': 'MANUSCRIPT_IMPORTED', 'projectRoot': plan['projectRoot'], 'projectId': plan['projectId'],
              'bookName': plan['bookName'], 'planId': plan['planId'], 'planHash': plan['planHash'],
              'chapterCount': len(mappings), 'fileCount': len(plan['files']), 'proseBytes': sum(row['bytes'] for row in mappings),
              'chapterStatus': 'planning_required', 'semanticReviewComplete': False, 'sourceFilesModified': False,
              'alreadyCommitted': False, 'receiptPath': RECEIPT,
              'pending': ['核对原稿并登记有证据的设定', '整理卷、弧线与章节规划', '续写前完成实际依赖的审阅与授权'],
              'pendingIsHistorical': True,
              'followupAction': {'operation': 'import.organization-status', 'arguments': {'projectRoot': plan['projectRoot']}},
              'verifyAction': {'operation': 'import.verify', 'arguments': {'projectRoot': plan['projectRoot']}},
              'nextAction': {'operation': 'import.organization-prepare', 'arguments': {'projectRoot': plan['projectRoot']}},
              'healthAction': {'operation': 'project.doctor', 'arguments': {'projectRoot': plan['projectRoot'], 'view': 'model'}}}
    from .command_help import cli_action
    for key in ('nextAction', 'healthAction', 'followupAction', 'verifyAction'):
        result[key] = cli_action(result[key]['operation'], result[key]['arguments'])
    write_json(staging / RECEIPT, {'kind': 'manuscript_import_receipt', 'planId': plan['planId'], 'planHash': plan['planHash'],
                                 'projectId': plan['projectId'], 'approvedImport': True, 'semanticReviewComplete': False,
                                 'plan': plan, 'chapters': mappings, 'result': result})
    return result


def commit_import(root: Path, *, planId: str, planHash: str, approve: bool = False) -> dict:
    if approve is not True:
        raise StructureError('APPROVAL_REQUIRED', 'explicit author approval of the import preview is required')
    root = root.expanduser().resolve()
    existing = _completed(root, planId, planHash)
    if existing is not None:
        return existing
    directory, plan = read_plan(root, planId, planHash)
    from .data_provider import binding
    if (plan['manifestHash'] != digest((TOOLBOX_ROOT / 'toolbox.manifest.json').read_bytes())
            or plan['provider'] != binding()):
        raise StructureError('IMPORT_PLAN_STALE', 'engine or local provider changed; preview again')
    _verify_sources(plan)
    staging = Path(tempfile.mkdtemp(prefix='build-', dir=directory))
    try:
        result = _build(staging, directory, plan)
        checkpoint('staged')
        # Persist the full tree before the only publication point. A crash before
        # rename leaves no project; a crash after it leaves the complete receipt.
        for folder, _, files in os.walk(staging, topdown=False):
            for name in files:
                with (Path(folder) / name).open('rb') as stream:
                    os.fsync(stream.fileno())
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        checkpoint('before-publication')
        _verify_sources(plan)
        exclusive_move(staging, root)
        checkpoint('after-publication')
        return result
    except FileExistsError:
        return _completed(root, planId, planHash)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
