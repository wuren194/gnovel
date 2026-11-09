"""Mechanical diagnostic evidence bound to the exact candidate prose.

Registering an artifact never approves it. Freshness and passing are separate:
a complete failing report can be reused until an input changes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .paths import stored_project_path
from .artifacts import envelope, file_sha256, sha256_json, sha256_text
from .jsonio import read_json
from .locks import project_lock_reentrant
from .project import TOOLBOX_ROOT, toolbox_manifest
from .review_artifacts import validate_artifact_binding
from .structure_store import StructureError, load_structure
from .workflow import default_workflow, normalize_workflow, record_artifact
from .write_sets import (apply_write_set, digest, guard_observation, inventory, json_bytes,
                         safe_project_path, validate_guards)

def implementation_fingerprint() -> dict:
    from .static_cache import cached_json, cached_sha256
    manifest = toolbox_manifest()
    registry = cached_json(TOOLBOX_ROOT / manifest['sharedProtocols']['toolRegistry'])
    paths = {row['path'] for row in registry['entrypoints']
             if row.get('id') == 'aggregate-diagnostics' or row.get('parent') == 'aggregate-diagnostics'}
    paths.update(str(path.relative_to(TOOLBOX_ROOT)) for path in (TOOLBOX_ROOT / 'scripts/text').glob('*.py'))
    paths.update(str(path.relative_to(TOOLBOX_ROOT)) for path in (TOOLBOX_ROOT / manifest['sharedProtocols']['registries']).glob('*.json'))
    paths.update(manifest['storage']['cacheDependencies'])
    paths.update(('scripts/core/mechanical_evidence.py', 'scripts/core/diagnostic_inputs.py', 'scripts/core/reports.py',
                  'scripts/core/artifacts.py', 'scripts/core/exit_codes.py', 'scripts/text_utils.py',
                  'scripts/intensity_registry.py', 'scripts/debug_validation.py',
                  'scripts/sepia_bridge.py', 'scripts/core/prose_calibration.py',
                  'scripts/core/writing_checks.py', 'scripts/core/writing_notes.py',
                  'scripts/core/outline_facts.py', 'scripts/core/chapter_contract.py', 'scripts/core/source_placement.py',
                  'scripts/core/outline_constraints.py', 'scripts/core/settings.py'))
    from .data_provider import binding
    return {**{path: cached_sha256(TOOLBOX_ROOT / path) for path in sorted(paths)},
            'localProvider': sha256_json(binding())}


def _registered(root: Path, structure: dict, workflow: dict, chapter_id: str, kind: str, *, historical: bool = False) -> dict | None:
    entry = workflow.get('artifacts', {}).get(chapter_id, {}).get(kind)
    if not isinstance(entry, dict) or (entry.get('invalidatedBy') and not historical) or not entry.get('artifactPath'):
        return None
    path = stored_project_path(root, entry['artifactPath'])
    data = read_json(path)
    validate_artifact_binding(data, project_id=structure['projectId'], chapter_id=chapter_id)
    if (data['artifactType'] != kind or data['artifactSha256'] != entry.get('artifactSha256')
            or file_sha256(path) != entry.get('artifactFileSha256')):
        raise StructureError('DIAGNOSTICS_DEPENDENCY_INVALID', 'registered artifact differs from its file', {'artifactType': kind})
    if kind == 'candidate_prose' and not historical:
        from .current_candidate import require_candidate_selection
        require_candidate_selection(root, structure, chapter_id, data['payload'])
        from .candidate_batch import require_artifact_recipe
        require_artifact_recipe(root, structure, workflow, chapter_id, data['payload'])
    return data


def candidate_prose(root: Path, chapter_id: str, *, structure: dict | None = None) -> dict:
    structure = load_structure(root) if structure is None else structure
    chapter = next((row for row in structure['chapters'] if row['chapterId'] == chapter_id), None)
    if chapter is None:
        raise StructureError('CHAPTER_NOT_FOUND', 'unknown chapter', {'chapterId': chapter_id})
    workflow_path = root / '.gnovel/workflow.json'
    workflow = normalize_workflow(read_json(workflow_path)) if workflow_path.is_file() else default_workflow()
    entry = workflow.get('artifacts', {}).get(chapter_id, {}).get('candidate_prose')
    from .current_candidate import require_candidate_selection
    if not isinstance(entry, dict):
        require_candidate_selection(root, structure, chapter_id, None)
    if isinstance(entry, dict):
        data = _registered(root, structure, workflow, chapter_id, 'candidate_prose')
        if data is None:
            raise StructureError('CANDIDATE_PROSE_MISSING', 'registered prose is invalidated or missing')
        path = stored_project_path(root, entry['artifactPath'])
        from .writing_notes import validate_candidate_binding
        binding = validate_candidate_binding(root, structure, workflow, chapter_id, data['payload'])
        selected = require_candidate_selection(root, structure, chapter_id, data['payload'])
        return {'text': data['payload']['text'], 'source': 'artifact',
                'artifactPath': str(path.relative_to(root)), 'artifactSha256': data['artifactSha256'],
                'beatSpans': data['payload'].get('beatSpans', []),
                'writingNotes': data['payload'].get('writingNotes', {}), 'planBinding': binding,
                'candidateSelection': selected['selection'], 'selectionGuards': selected['guards']}
    path = safe_project_path(root, chapter['filePath'])
    if path.is_file():
        with path.open(encoding='utf-8', newline='') as handle:
            text = handle.read()
        if text.strip():
            return {'text': text, 'source': 'prose', 'artifactPath': str(path.relative_to(root)), 'artifactSha256': None}
    raise StructureError('CANDIDATE_PROSE_MISSING', 'no candidate prose or committed body to diagnose', {'chapterId': chapter_id})


def evidence_payload(root: Path, chapter_id: str, *, structure: dict | None = None) -> dict:
    from .consistency import require_no_pending
    from .settings_schema import settings_schema
    from .derived_views import source_files
    require_no_pending(root)
    structure = load_structure(root) if structure is None else structure
    prose = candidate_prose(root, chapter_id, structure=structure)
    return {'draftSha256': sha256_text(prose['text']), 'structureRevision': structure['revision'],
            'candidateArtifactSha256': prose['artifactSha256'],
            'structureHash': sha256_json(structure), 'sourceFiles': source_files(root),
            'settingsSchemaSha256': sha256_json(settings_schema()),
            'implementation': implementation_fingerprint(), 'proseSource': prose['source']}


def current_semantic_inputs(root, chapter_id, structure, facts=None):
    from .diagnostic_inputs import semantic_inputs
    facts = evidence_payload(root, chapter_id, structure=structure) if facts is None else facts
    return semantic_inputs(root, structure, chapter_id, candidate_prose(root, chapter_id, structure=structure), facts)


def complete_evidence(payload: dict) -> bool:
    """Only a normally completed, fully retained report is reusable."""
    return (type(payload.get('passed')) is bool
            and isinstance(payload.get('run'), dict)
            and isinstance(payload.get('findings'), list)
            and isinstance(payload.get('lineDetails'), dict)
            and isinstance(payload['lineDetails'].get('blocks'), dict)
            and not payload['lineDetails'].get('error')
            and payload['lineDetails'].get('__returncode') in (0, 1, 2)
            and isinstance(payload.get('aggregateInputHash'), str)
            and isinstance(payload.get('checksPresent'), list)
            and all(isinstance(check, str) for check in payload['checksPresent'])
            and set(payload['checksPresent']) == {
                'ai_cliche_linter', 'validate_text', 'check_voice_lexicon', 'check_degeneration'})


def evidence_current(root: Path, chapter_id: str, *, structure: dict | None = None) -> dict:
    structure = load_structure(root) if structure is None else structure
    workflow_path = root / '.gnovel/workflow.json'
    workflow = normalize_workflow(read_json(workflow_path)) if workflow_path.is_file() else default_workflow()
    entry = workflow.get('artifacts', {}).get(chapter_id, {}).get('diagnostics')
    result = {'registered': isinstance(entry, dict), 'current': False, 'fresh': False, 'passed': False}
    if not isinstance(entry, dict) or entry.get('invalidatedBy'):
        return {**result, 'reason': 'no current diagnostics artifact is registered'}
    try:
        data = _registered(root, structure, workflow, chapter_id, 'diagnostics')
        if data is None:
            return {**result, 'reason': 'diagnostics artifact is unavailable'}
        payload = data['payload']
        facts = evidence_payload(root, chapter_id, structure=structure)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {**result, 'reason': str(exc)}
    result['passed'] = payload.get('passed') is True
    if payload.get('draftSha256') != facts['draftSha256']:
        return {**result, 'reason': 'candidate prose changed'}
    if payload.get('inputHash') is None or not isinstance(payload.get('run'), dict):
        return {**result, 'reason': 'diagnostic evidence has no complete input fingerprint'}
    if not complete_evidence(payload):
        return {**result, 'reason': 'diagnostic evidence is incomplete and cannot be reused'}
    from .diagnostic_inputs import stored_input_proof
    if not stored_input_proof(payload):
        return {**result, 'reason': 'diagnostic consumer input proof is missing or corrupt; rerun diagnostics'}
    semantic = current_semantic_inputs(root, chapter_id, structure, facts)
    if sha256_json({**semantic, **payload['run']}) != payload['semanticInputHash']:
        return {**result, 'reason': 'consumed chapter, voice, settings, rules or implementation changed after the run'}
    return {**result, 'current': result['passed'], 'fresh': True,
            'reason': 'evidence matches current inputs' if result['passed'] else 'current mechanical report requires repair'}


def run_diagnostics(root: Path, *, chapter_id: str, mode: str | None = None, intensity_level: int | None = None, scene_paragraphs: str = '',
                    strict_floor: bool = False, register: bool = False, advance_lifecycle: bool = False,
                    _expected_candidate_sha256: str | None = None, view: str = 'full') -> dict:
    if view not in {'full', 'model'}:
        raise StructureError('DIAGNOSTICS_VIEW_INVALID', 'view must be full or model')
    result = _run_diagnostics(root, chapter_id=chapter_id, mode=mode, intensity_level=intensity_level,
        scene_paragraphs=scene_paragraphs, strict_floor=strict_floor, register=register,
        advance_lifecycle=advance_lifecycle, _expected_candidate_sha256=_expected_candidate_sha256)
    if view == 'full':
        return result
    from .transport_cache import put
    payload = result['evidence']
    compact = {k: v for k, v in result.items() if k not in {'evidence', 'artifact', 'diff'}}
    if result.get('artifact'):
        compact['artifactSha256'] = result['artifact']['artifactSha256']
    compact.update({k: payload.get(k) for k in ('passed', 'summary', 'recommendation', 'draftSha256',
        'candidateArtifactSha256', 'run', 'planBinding', 'writingNotes', 'chapterContract', 'voiceCoverage')})
    if isinstance(compact.get('voiceCoverage'), dict):
        compact['voiceCoverage'] = {k: v for k, v in compact['voiceCoverage'].items() if k != 'dialogueAttributions'}
        compact['voiceCoverage']['attributionCount'] = len(payload['voiceCoverage'].get('dialogueAttributions', []))
    compact['findingCount'] = len(payload['findings'])
    compact['reportRef'] = put(root, 'diagnostics', result.get('artifact') or result)
    if result.get('diff') is not None:
        compact['diff'] = {'summary': result['diff'].get('summary'),
                           'reference': put(root, 'diagnostics-diff', result['diff'])}
    return compact


def _run_diagnostics(root: Path, *, chapter_id: str, mode: str | None = None, intensity_level: int | None = None,
                     scene_paragraphs: str = '', strict_floor: bool = False, register: bool = False,
                     advance_lifecycle: bool = False, _expected_candidate_sha256: str | None = None) -> dict:
    root = root.expanduser().resolve()
    from .data_provider import binding as provider_binding, pinned_environment
    expected_provider = provider_binding()
    with project_lock_reentrant(root):
        structure = load_structure(root)
        if advance_lifecycle and not register:
            raise StructureError('LIFECYCLE_ADVANCE_INVALID', 'advancement requires diagnostics registration')
        inference_guard = None
        from .outline_facts import inspect_outline
        outline = inspect_outline(root, chapterId=chapter_id, structure=structure)
        if outline.get('available'):
            state, _ = guard_observation(safe_project_path(root, outline['path']))
            if not outline.get('sourceMode') and state['sha256'] != outline['sha256']:
                raise StructureError('DIAGNOSTICS_INPUT_CHANGED', 'outline changed during parameter extraction')
            inference_guard = {'path': outline['path'], **state}
        mode = outline.get('mode') if mode is None else mode
        prose = candidate_prose(root, chapter_id, structure=structure)
        from .writing_notes import proposed_level, validate_notes
        notes = prose.get('writingNotes', {})
        validate_notes(notes, prose['text'])
        effective_level = proposed_level(notes, outline.get('intensityLevel'))
        if intensity_level is not None and effective_level is not None and intensity_level != effective_level:
            raise StructureError('DIAGNOSTICS_UNDECLARED_DEVIATION', 'intensityLevel differs from the plan; register a writing note with the candidate first')
        intensity_level = effective_level if intensity_level is None else intensity_level
        if outline.get('mode') is not None and mode != outline['mode']:
            raise StructureError('DIAGNOSTICS_PLAN_MODE_MISMATCH', 'mode differs from the registered chapter plan')
        if _expected_candidate_sha256 is not None and prose['artifactSha256'] != _expected_candidate_sha256:
            raise StructureError('DIAGNOSTICS_CANDIDATE_CHANGED', 'selected packed candidate is no longer current')
        facts = evidence_payload(root, chapter_id, structure=structure)
        run = {'mode': mode, 'intensityLevel': intensity_level, 'sceneParagraphs': scene_paragraphs, 'strictFloor': strict_floor,
               'plannedDirtyLevel': outline.get('intensityLevel'), 'deviationPendingReview': bool(notes.get('deviations'))}
        try:
            from scripts.intensity_registry import level_bounds
        except ImportError:
            from intensity_registry import level_bounds
        low, high = level_bounds()
        if mode not in ('plot', 'buildUp', 'mixed', 'peakScene') or type(intensity_level) is not int or not low <= intensity_level <= high:
            raise StructureError('DIAGNOSTICS_INPUT_INVALID', 'explicit valid mode and intensityLevel are required')
        if mode == 'mixed' and not scene_paragraphs:
            raise StructureError('DIAGNOSTICS_INPUT_INVALID', 'mixed mode requires explicit sceneParagraphs')
        if strict_floor and mode != 'peakScene' and not scene_paragraphs:
            raise StructureError('DIAGNOSTICS_INPUT_INVALID', 'strict floor on a partly adult chapter requires explicit sceneParagraphs')
        workflow_path = safe_project_path(root, '.gnovel/workflow.json')
        workflow_raw = workflow_path.read_bytes() if workflow_path.is_file() else None
        workflow = normalize_workflow(json.loads(workflow_raw)) if workflow_raw is not None else default_workflow()
        proposed = None
        if advance_lifecycle:
            status = next(r['status'] for r in structure['chapters'] if r['chapterId'] == chapter_id)
            if status == 'draft':
                from .chapter_state import prepare_transition
                proposed, _ = prepare_transition(root, structure, workflow, chapter_id, 'review',
                                                  actor='diagnostics', pending_diagnostics=True)
            elif status != 'review':
                raise StructureError('LIFECYCLE_ADVANCE_INVALID', 'diagnostics advancement requires draft or review')
        current_facts = facts
        if proposed is not None:
            facts = evidence_payload(root, chapter_id, structure=proposed)
            if '.gnovel/structure.json' in facts['sourceFiles']:
                facts['sourceFiles']['.gnovel/structure.json'] = {'sha256': digest(json_bytes(proposed))}
        previous_entry = workflow.get('artifacts', {}).get(chapter_id, {}).get('diagnostics')
        previous = None
        try:
            previous = _registered(root, structure, workflow, chapter_id, 'diagnostics', historical=True)
        except (ValueError, OSError, KeyError, TypeError):
            pass
        input_hash = sha256_json({**facts, **run})
        semantic = current_semantic_inputs(root, chapter_id, proposed or structure, facts)
        semantic_hash = sha256_json({**semantic, **run})
        guards = [inference_guard] if inference_guard else []
        guards.extend(prose.get('selectionGuards', []))
        for relative in sorted({'.gnovel/structure.json', prose['artifactPath'],
                                *facts['sourceFiles']}):
            state, _ = guard_observation(safe_project_path(root, relative))
            guards.append({'path': relative, **state})
        from .diagnostic_inputs import stored_input_proof
        if (previous is not None and not previous_entry.get('invalidatedBy')
                and stored_input_proof(previous['payload'])
                and previous['payload'].get('semanticInputHash') == semantic_hash
                and complete_evidence(previous['payload'])):
            if proposed is not None:
                # Reuse the original report, but commit the explicitly requested
                # lifecycle transition with the same CAS/guard rules as a new run.
                relative = stored_project_path(root, previous_entry['artifactPath']).relative_to(root).as_posix()
                state, _ = guard_observation(safe_project_path(root, relative))
                guards.append({'path': relative, **state})
                if _registered(root, structure, workflow, chapter_id, 'diagnostics', historical=True) != previous:
                    raise StructureError('DIAGNOSTICS_INPUT_CHANGED', 'reused report changed before lifecycle advancement')
                if current_facts != evidence_payload(root, chapter_id):
                    raise StructureError('DIAGNOSTICS_INPUT_CHANGED', 'diagnostic inputs changed before lifecycle advancement')
                from .derived_views import derived_outputs
                outputs, _ = derived_outputs(root, proposed, current=structure,
                    extras={'.gnovel/workflow.json': json_bytes(workflow)})
                _publish(root, outputs, guards, workflow_raw)
            return {'ok': True, 'code': 'DIAGNOSTICS_REUSED', 'evidence': previous['payload'], 'artifact': previous,
                    'registered': True, 'approved': False, 'reused': True,
                    'artifactPath': stored_project_path(root, previous_entry['artifactPath']).relative_to(root).as_posix(),
                    'previousArtifactPath': previous_entry.get('previousArtifactPath'), 'proseSource': prose['source'], 'approvalPerformed': False}
        try:
            from scripts import state_cache
        except ImportError:
            import state_cache
        state_cache.rebuild(root)
        design_arg = state_cache._cache_paths(root)[1]
        with tempfile.TemporaryDirectory() as temp:
            draft = Path(temp) / f'{chapter_id}.md'
            draft.write_bytes(prose['text'].encode('utf-8'))
            command = [sys.executable, str(TOOLBOX_ROOT / 'scripts/aggregate_diagnostics.py'), str(draft),
                       '--intensity-level', str(intensity_level), '--mode', mode, '--json', '--line-details', '--design', str(design_arg),
                       '--project-root', str(root)]
            if strict_floor:
                command.append('--strict-floor')
            if scene_paragraphs:
                command += ['--scene-paragraphs', scene_paragraphs]
            result = subprocess.run(command, capture_output=True, text=True, check=False,
                                    env=pinned_environment(expected_provider))
        if result.returncode not in (0, 1, 2) or not result.stdout.strip():
            raise StructureError('DIAGNOSTICS_FAILED', 'mechanical diagnostics did not complete',
                                 {'exitCode': result.returncode, 'stderr': result.stderr[-2000:]})
        report = json.loads(result.stdout)
        if (report.get('providerBinding') != expected_provider
                or facts['implementation']['localProvider'] != sha256_json(expected_provider)):
            raise StructureError('DIAGNOSTICS_INPUT_CHANGED', 'diagnostics used a different provider snapshot')
        if (report.get('draftSha256') != facts['draftSha256'] or not report.get('inputHash')
                or not isinstance(report.get('lineDetails'), dict)
                or not isinstance(report.get('findings'), list) or type(report.get('passed')) is not bool):
            raise StructureError('DIAGNOSTICS_UNBOUND', 'diagnostics report is incomplete or unbound')
        from .writing_checks import audit_chapter
        contract = audit_chapter(root, structure, chapter_id, prose['text'], prose.get('beatSpans', []))
        for finding in contract['findings']:
            report['findings'].append(finding)
            severity = finding['severity']
            report['summary']['perSeverity'][severity] = report['summary']['perSeverity'].get(severity, 0) + 1
        report['summary']['totalFindings'] = len(report['findings'])
        report['summary']['perScript']['chapter_contract'] = len(contract['findings'])
        if not contract['passed']:
            from .reports import best_recommendation
            report['passed'] = False
            report['aggregatedRecommendation'] = best_recommendation([report['aggregatedRecommendation'], 'surgical_edit'])
        payload = {'passed': report['passed'], 'draftSha256': facts['draftSha256'],
                   'candidateArtifactSha256': prose['artifactSha256'], 'planBinding': prose.get('planBinding'),
                   'chapterContract': contract, 'writingNotes': notes,
                   'voiceCoverage': report['scriptResults']['check_voice_lexicon'].get('coverage', {}),
                   'recommendation': report['aggregatedRecommendation'], 'findings': report['findings'],
                   'calibration': report.get('calibration', {}),
                   'summary': report.get('summary', {}), 'lineDetails': report['lineDetails'], 'run': run,
                   'aggregateInputHash': report['inputHash'],
                   'checksPresent': sorted(key for key, value in report['scriptResults'].items()
                                           if value and value.get('status') != 'skipped'), 'inputHash': input_hash,
                   'runInputs': facts, 'semanticInputs': semantic, 'semanticInputHash': semantic_hash}
        if not complete_evidence(payload):
            raise StructureError('DIAGNOSTICS_INCOMPLETE', 'all checks and line details must complete')
        artifact = envelope('diagnostics', payload, project_id=structure['projectId'], chapter_id=chapter_id,
                            source_sha256=prose['artifactSha256'])
        validate_guards(root, guards, stage='diagnostics-result')
        if current_facts != evidence_payload(root, chapter_id):
            raise StructureError('DIAGNOSTICS_INPUT_CHANGED', 'diagnostic inputs changed during the run')
        previous_path = previous_entry.get('artifactPath') if previous is not None else None
        diff = None
        if previous is not None:
            from .diagnostics_diff import diff_findings, model_diff
            diff = model_diff(diff_findings(previous['payload'].get('findings', []), payload['findings']))
        if not register:
            return {'ok': True, 'code': 'DIAGNOSTICS_EVIDENCE', 'evidence': payload, 'artifact': artifact,
                    'registered': False, 'approved': False, 'reused': False, 'previousArtifactPath': previous_path,
                    'diff': diff, 'proseSource': prose['source']}
        relative = f'工作台/{chapter_id}-diagnostics-{artifact["artifactSha256"]}.json'
        artifact_bytes = json_bytes(artifact)
        record_artifact(workflow, chapter_id, artifact, structure=structure, artifact_path=str(root / relative),
                        project_id=structure['projectId'], project_root=root, artifact_bytes=artifact_bytes)
        if previous_path:
            workflow['artifacts'][chapter_id]['diagnostics']['previousArtifactPath'] = previous_path
        outputs = {relative: artifact_bytes, '.gnovel/workflow.json': json_bytes(workflow)}
        if proposed is not None:
            from .derived_views import derived_outputs
            outputs, _ = derived_outputs(root, proposed, current=structure, extras=outputs)
        _publish(root, outputs, guards, workflow_raw)
        return {'ok': True, 'code': 'DIAGNOSTICS_REGISTERED', 'evidence': payload, 'registered': True,
                'approved': False, 'reused': False, 'artifactPath': relative, 'previousArtifactPath': previous_path,
                'artifactSha256': artifact['artifactSha256'],
                'diff': diff, 'approvalPerformed': False, 'note': '登记不是批准；语义审查与作者批准仍须独立完成'}


def _publish(root, outputs, guards, workflow_raw):
    expected = inventory(root, outputs)
    observed = next(row['before']['sha256'] for row in expected if row['path'] == '.gnovel/workflow.json')
    if observed != (digest(workflow_raw) if workflow_raw is not None else None):
        raise StructureError('WRITE_SET_CONFLICT', 'workflow changed during diagnostics')
    validate_guards(root, guards, stage='diagnostics-publication')
    by_path = {g['path']: g for g in guards}
    for row in expected:
        if row['path'] in by_path and row['before']['sha256'] != by_path[row['path']]['sha256']:
            raise StructureError('WRITE_SET_CONFLICT', 'lifecycle input changed before publication')
    apply_write_set(root, outputs, expected=expected,
                    guards=[g for g in guards if g['path'] not in outputs], operation='diagnostics_register')
