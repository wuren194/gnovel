"""Frozen external-edit review materials. No adoption or approval capability.

Only chapter-associated, verified historical images may supply H1. H2 is the
exact descriptor observation, never a later reread; live prose remains a guard.
These are attachments of the original source request, not a new fact ledger.
"""
from __future__ import annotations
import difflib
import json
from pathlib import Path

from .paths import stored_project_path
from .artifacts import validate_envelope
from .file_preservation import observation
from .jsonio import read_json
from .prose_binding import validate_binding
from .structure_store import StructureError
from .write_sets import (GUARD_KEYS, apply_write_set, digest, guard_state, json_bytes,
                         safe_project_path, validate_guards)


MATERIAL_PROTOCOL = 'external-review-material-v1'


def _matching_chapter(source, current, chapter) -> bool:
    if not isinstance(source, dict) or source.get('projectId') != current['projectId']:
        return False
    row = next((row for row in source.get('chapters', []) if row.get('chapterId') == chapter['chapterId']), None)
    return bool(row and row.get('filePath') == chapter['filePath']
                and row.get('proseBinding') == chapter['proseBinding'] and not validate_binding(source, row))


def _accepted_bytes(root: Path, source: dict, chapter: dict) -> tuple[bytes | None, dict | None]:
    expected = chapter['proseBinding']['expectedContentDigest']
    relative = chapter['filePath']
    # The registered artifact is usable only when the existing finalization record
    # binds its exact identity. A same-hash unrelated manuscript is not provenance.
    workflow_path = safe_project_path(root, '.govel/workflow.json')
    workflow = read_json(workflow_path) if workflow_path.is_file() else {}
    approval = workflow.get('approvals', {}).get(chapter['chapterId'], {})
    entry = workflow.get('artifacts', {}).get(chapter['chapterId'], {}).get('candidate_prose', {})
    if (approval.get('finalized') is True and entry.get('artifactSha256')
            and approval.get('artifactHashes', {}).get('candidate_prose') == entry['artifactSha256']):
        try:
            artifact = validate_envelope(read_json(stored_project_path(root, entry['artifactPath'])),
                                         artifact_type='candidate_prose')
            payload = artifact['payload']['text'].encode('utf-8')
            if (artifact.get('projectId') == source['projectId'] and artifact.get('chapterId') == chapter['chapterId']
                    and artifact['artifactSha256'] == entry['artifactSha256'] and 'sha256:' + digest(payload) == expected):
                return payload, {'kind': 'finalized_artifact', 'artifactSha256': artifact['artifactSha256'],
                                 'path': entry['artifactPath']}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    # Current transaction archives contain immutable after-images together with
    # the actual structure/ownership image committed by that same transaction.
    directory = safe_project_path(root, '.govel/transactions')
    for terminal in sorted(directory.glob('TX-*/completed.json'), reverse=True):
        try:
            manifest = read_json(safe_project_path(root, terminal.relative_to(root)))
            if (manifest.get('phase') != 'committed'
                    or manifest.get('publicationProtocol') != 'preserve-exclusive-v1'):
                continue
            rows = {row['path']: row for row in manifest['files']}
            for side in ('after', 'before'):
                source_row = rows.get('.govel/structure.json', {}).get(side, {})
                prose_row = rows.get(relative, {}).get(side, {})
                if not source_row.get('exists') or not prose_row.get('exists'):
                    continue
                source_path = safe_project_path(root, terminal.parent.relative_to(root)/side/'.govel/structure.json')
                prose_path = safe_project_path(root, terminal.parent.relative_to(root)/side/relative)
                source_bytes, payload = source_path.read_bytes(), prose_path.read_bytes()
                if (digest(source_bytes) == source_row['sha256'] and digest(payload) == prose_row['sha256']
                        and 'sha256:' + digest(payload) == expected
                        and _matching_chapter(json.loads(source_bytes), source, chapter)):
                    return payload, {'kind': 'committed_transaction_image', 'transactionId': manifest['transactionId'],
                                     'side': side, 'path': str(prose_path.relative_to(root))}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Existing operation backups preserve a structure image that identifies the
    # same generation, chapter and locator; raw hash-only files are not searched.
    for journal_path in sorted(safe_project_path(root, '.govel/operations').glob('OP-*.json'), reverse=True):
        try:
            journal = read_json(safe_project_path(root, journal_path.relative_to(root)))
            if journal.get('schemaVersion') != 4 or journal.get('operationId') != journal_path.stem:
                continue
            rows = journal.get('beforeFiles', {})
            if not rows.get(relative, {}).get('exists') or not rows.get('.govel/structure.json', {}).get('exists'):
                continue
            backup = safe_project_path(root, journal['backupPath'])
            source_path = safe_project_path(root, backup.relative_to(root)/'structure.json')
            prose_path = safe_project_path(root, backup.relative_to(root)/relative)
            source_bytes, payload = source_path.read_bytes(), prose_path.read_bytes()
            if (digest(source_bytes) == rows['.govel/structure.json']['sha256']
                    and digest(payload) == rows[relative]['sha256'] and 'sha256:' + digest(payload) == expected
                    and _matching_chapter(json.loads(source_bytes), source, chapter)):
                return payload, {'kind': 'operation_before_image', 'operationId': journal['operationId'],
                                 'path': str(prose_path.relative_to(root))}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None, None


def freeze_external_materials(root: Path, source: dict, chapter: dict, operation_id: str, *,
                              request_fingerprint: str) -> tuple[dict, list[dict]]:
    live = safe_project_path(root, chapter['filePath'])
    observed, h2 = observation(live)
    if h2 is None:
        raise StructureError('EXTERNAL_REVIEW_BYTES_UNAVAILABLE', 'external prose is not a readable regular file')
    prefix = f'.govel/plans/{operation_id}.external'
    prior_path = safe_project_path(root, f'{prefix}/material.json')
    if prior_path.is_file():
        prior = read_json(prior_path)
        if (prior.get('requestFingerprint') != request_fingerprint or prior.get('observedObject') != observed
                or prior.get('projectId') != source['projectId'] or prior.get('chapterId') != chapter['chapterId']
                or prior.get('bindingGeneration') != chapter['proseBinding']['generation']
                or prior.get('expectedContentDigest') != chapter['proseBinding']['expectedContentDigest']):
            raise StructureError('EXTERNAL_REVIEW_MATERIAL_CONFLICT', 'uncommitted request retains different frozen inputs')
        paths = [prior_path.relative_to(root).as_posix(), *[prior[name]['path'] for name in ('H1', 'H2', 'diff') if prior.get(name)]]
        for name in ('H1', 'H2', 'diff'):
            if prior.get(name) and digest(safe_project_path(root, prior[name]['path']).read_bytes()) != prior[name]['sha256']:
                raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'frozen request material changed', {'part': name})
        guards = [{'path': chapter['filePath'], **{key: observed[key] for key in ('exists', 'sha256', 'dev', 'ino')}},
                  *[{'path': relative, **guard_state(safe_project_path(root, relative))} for relative in paths]]
        return {**prior, 'metadataPath': str(prior_path.relative_to(root))}, guards
    h1, provenance = _accepted_bytes(root, source, chapter)
    h2_path = f'{prefix}/H2.md'
    guards = [{'path': chapter['filePath'], **{key: observed[key] for key in ('exists', 'sha256', 'dev', 'ino')}}]
    missing = ['independent_user_presence_capability', 'trusted_display_and_pin', 'semantic_review']
    if h1 is None:
        missing.append('accepted_H1_bytes')
    material = {'protocol': MATERIAL_PROTOCOL, 'operationId': operation_id, 'requestFingerprint': request_fingerprint,
        'projectRoot': str(root.resolve()), 'projectId': source['projectId'], 'chapterId': chapter['chapterId'],
        'filePath': chapter['filePath'], 'bindingGeneration': chapter['proseBinding']['generation'],
        'expectedContentDigest': chapter['proseBinding']['expectedContentDigest'],
        'observedContentDigest': 'sha256:' + observed['sha256'], 'observedObject': observed,
        'availability': 'complete' if h1 is not None else 'missing_accepted_bytes',
        'H1': {'path': f'{prefix}/H1.md', 'sha256': digest(h1), 'provenance': provenance} if h1 is not None else None,
        'H2': {'path': h2_path, 'sha256': digest(h2)},
        'semanticReview': {'status': 'not_performed', 'covered': [], 'findings': [],
                           'unknown': ['meaning', 'causality', 'continuity', 'downstream_dependencies']},
        'adoptionExecutable': False, 'missingPrerequisites': missing}
    outputs = {h2_path: h2}
    if h1 is not None:
        outputs[f'{prefix}/H1.md'] = h1
        try:
            diff = ''.join(difflib.unified_diff(h1.decode('utf-8').splitlines(keepends=True),
                h2.decode('utf-8').splitlines(keepends=True), fromfile='accepted H1', tofile='reviewed H2'))
            outputs[f'{prefix}/diff.patch'] = diff.encode('utf-8')
            material['diff'] = {'path': f'{prefix}/diff.patch', 'sha256': digest(outputs[f'{prefix}/diff.patch'])}
        except UnicodeDecodeError:
            material['missingPrerequisites'].append('utf8_diff')
    metadata = f'{prefix}/material.json'
    outputs[metadata] = json_bytes(material)
    for relative, payload in outputs.items():
        path = safe_project_path(root, relative)
        if path.exists() and path.read_bytes() != payload:
            raise StructureError('EXTERNAL_REVIEW_MATERIAL_CONFLICT', 'frozen material already exists with different bytes',
                                 {'path': relative})
    apply_write_set(root, outputs, operation='external_review_materials', guards=guards)
    for relative in outputs:
        guards.append({'path': relative, **guard_state(safe_project_path(root, relative))})
    material['metadataPath'] = metadata
    return material, guards


def validate_external_materials(root: Path, plan: dict, *, stage: str) -> None:
    if plan.get('operation') != 'accept_external_modification':
        return
    material = plan.get('externalReviewMaterials')
    if (not isinstance(material, dict) or material.get('protocol') != MATERIAL_PROTOCOL
            or material.get('operationId') != plan.get('operationId')
            or material.get('projectRoot') != str(root.resolve())):
        raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'missing frozen external-edit material')
    for key in ('chapterId', 'filePath', 'bindingGeneration', 'expectedContentDigest', 'observedContentDigest'):
        if material.get(key) != plan['parameters'].get(key):
            raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'material and plan identify different inputs', {'field': key})
    frozen = read_json(safe_project_path(root, material['metadataPath']))
    if frozen != {key: value for key, value in material.items() if key != 'metadataPath'}:
        raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'frozen material metadata changed')
    for name in ('H1', 'H2', 'diff'):
        row = material.get(name)
        if row and digest(safe_project_path(root, row['path']).read_bytes()) != row['sha256']:
            raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'frozen bytes changed', {'part': name})
    guards = plan.get('externalReviewGuards')
    if not isinstance(guards, list) or not guards:
        raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'external object guard is missing')
    expected_paths = {material['filePath'], material['metadataPath'],
                      *[material[name]['path'] for name in ('H1', 'H2', 'diff') if material.get(name)]}
    if (any(not isinstance(row, dict) or set(row) != {'path', *GUARD_KEYS} for row in guards)
            or len(guards) != len(expected_paths) or {row['path'] for row in guards} != expected_paths):
        raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'all original prose and frozen material guards are required')
    indexed = {row['path']: row for row in guards}
    live = indexed[material['filePath']]
    if any(live[key] != material['observedObject'][key] for key in GUARD_KEYS):
        raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'live manuscript guard differs from reviewed H2')
    for name in ('H1', 'H2', 'diff'):
        row = material.get(name)
        if row and indexed[row['path']]['sha256'] != row['sha256']:
            raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'material guard and frozen content differ', {'part': name})
    validate_guards(root, guards, stage=stage)
    if {row['path'] for row in guards} & {row['path'] for row in plan.get('writeSet', [])}:
        raise StructureError('EXTERNAL_REVIEW_GUARD_IS_OUTPUT', 'reviewed prose and materials must remain read-only')
