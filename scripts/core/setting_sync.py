"""Read a registered state candidate against current source projections."""
from __future__ import annotations

from pathlib import Path

from .artifacts import file_sha256, sha256_json
from .jsonio import read_json
from .paths import stored_project_path
from .review_artifacts import validate_artifact_binding
from .structure_store import StructureError, load_structure


def state_preview(root: Path, chapter_id: str) -> dict:
    """Inspect bindings only; source changes have their own reviewed transaction preview."""
    root = root.expanduser().resolve()
    structure = load_structure(root)
    workflow = read_json(root/'.gnovel/workflow.json')
    chapter = next((row for row in structure['chapters'] if row['chapterId'] == chapter_id), None)
    if chapter is None:
        raise StructureError('CHAPTER_NOT_FOUND', 'unknown chapter', {'chapterId': chapter_id})
    entry = workflow.get('artifacts', {}).get(chapter_id, {}).get('state_candidate')
    if not entry or not entry.get('artifactPath') or entry.get('invalidatedBy'):
        raise StructureError('STATE_CANDIDATE_MISSING', 'a current state candidate must be registered')
    path = stored_project_path(root, entry['artifactPath'])
    artifact = read_json(path)
    if file_sha256(path) != entry.get('artifactFileSha256'):
        raise StructureError('ARTIFACT_FILE_CHANGED', 'registered state candidate file changed')
    validate_artifact_binding(artifact, project_id=structure['projectId'], chapter_id=chapter_id)
    if artifact['artifactSha256'] != entry.get('artifactSha256'):
        raise StructureError('ARTIFACT_HASH_MISMATCH', 'registered state candidate identity changed')
    payload = artifact['payload']
    if payload.get('changes') != [] or payload.get('proposedFacts'):
        raise StructureError('SOURCE_STATE_CHANGE_REQUIRES_TRANSACTION',
                             'source changes require explicit source preview and commit')
    try:
        from scripts import state_cache
    except ImportError:
        import state_cache
    state, design = state_cache.project_projections(root)
    from .source_plan import chapter_plan
    current = {'baseStateSha256': sha256_json(state), 'designSha256': sha256_json(design),
               'sourcePlanHash': chapter_plan(structure, chapter_id)['sourcePlanHash']}
    return {'ok': True, 'code': 'STATE_PREVIEW', 'chapterId': chapter_id,
            'artifactSha256': artifact['artifactSha256'], 'bindings': current,
            'baseMatches': all(payload.get(key) == value for key, value in current.items()),
            'changes': [], 'sourceChangesOperation': 'source.change-preview',
            'semanticReviewComplete': False, 'approved': False}
