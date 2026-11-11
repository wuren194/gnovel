from __future__ import annotations

from pathlib import Path

from scripts import state_cache
from scripts.core.artifacts import envelope, file_sha256, sha256_json, sha256_text
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.jsonio import read_json, write_json
from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS
from scripts.core.structure_store import save_structure
from scripts.core.workflow import approve_artifacts, default_workflow, record_artifact
from tests.fixtures_current import create_current_project


def evidenced_slots(text: str) -> dict:
    """Synthetic mechanical evidence for workflow tests, not a literary review."""
    return {slot: {'conclusion': '合成夹具已核对当前文本', 'evidence': [{'text': text}]}
            for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES)}


def assessed_review_payload(structure, chapter_id, kind, payload):
    """Explicit synthetic judgments for unrelated workflow tests, never runtime defaults."""
    from copy import deepcopy
    from scripts.core.source_plan import chapter_plan
    payload = deepcopy(payload)
    if kind not in {'stage_a_review', 'stage_b_review'}:
        return payload
    payload.setdefault('behaviorCoverage', {'relationship': 'not_applicable', 'living': 'not_applicable'})
    payload.setdefault('sourcePlanHash', chapter_plan(structure, chapter_id)['sourcePlanHash'])
    if kind == 'stage_b_review':
        payload.setdefault('factAssessment', {'status': 'no_change'})
    else:
        text = '\n\n'.join(f['text'] for f in structure['fragments'] if f['ownerId'] == chapter_id and f['contentRole'] == 'plan' and f.get('status') != 'archived')
        payload.setdefault('seatReviews', {str(seat): {'conclusion': '合成夹具：已核对当前计划，无承重关系选择或持续生活安排。', 'evidence': [{'text': text}]} for seat in payload['activeSeats']})
    return payload


def register_and_approve(root: Path, chapter_id: str, kind: str, payload: dict) -> dict:
    structure = read_json(root / ".govel/structure.json")
    workflow = read_json(root / ".govel/workflow.json")
    if kind == 'candidate_prose':
        # Fixture authors supply current machine bindings exactly as artifact-pack
        # does; this does not manufacture any review conclusion or approval.
        from copy import deepcopy
        from scripts.core.source_plan import chapter_plan
        payload = deepcopy(payload)
        payload.setdefault('sourcePlanHash', chapter_plan(structure, chapter_id)['sourcePlanHash'])
        stage_a = workflow.get('artifacts', {}).get(chapter_id, {}).get('stage_a_review')
        if stage_a:
            payload.setdefault('stageAArtifactSha256', stage_a['artifactSha256'])
    payload = assessed_review_payload(structure, chapter_id, kind, payload)
    artifact = envelope(kind, payload, project_id=structure["projectId"], chapter_id=chapter_id)
    path = root / "工作台" / f"{chapter_id}-{kind}.json"
    write_json(path, artifact)
    record_artifact(workflow, chapter_id, artifact, structure=structure, artifact_path=str(path),
                    project_id=structure["projectId"], project_root=root)
    approve_artifacts(workflow, chapter_id, artifact_hashes={kind: artifact["artifactSha256"]}, approval_mode="manual", project_root=root)
    write_json(root / ".govel/workflow.json", workflow)
    return artifact



def run_and_approve_diagnostics(root: Path, chapter_id: str) -> dict:
    """Current-evidence tests use a real complete runner report, not legacy stubs."""
    from scripts.core.mechanical_evidence import run_diagnostics
    result = run_diagnostics(root, chapter_id=chapter_id, mode='plot', intensity_level=1, register=True)
    artifact = read_json(root / result['artifactPath'])
    workflow = read_json(root / '.govel/workflow.json')
    approve_artifacts(workflow, chapter_id, artifact_hashes={'diagnostics': artifact['artifactSha256']},
                      approval_mode='manual', project_root=root)
    write_json(root / '.govel/workflow.json', workflow)
    return result


def review_ready_project(root: Path, *, chapter_count: int = 2, changes: list | None = None) -> dict:
    """Current source fixture walked through the actual authoring gates."""
    from scripts.core.artifact_pack import pack_artifact
    from scripts.core.prose_binding import seal_binding
    from scripts.core.source_plan import chapter_plan
    if (root / '.govel/project.json').is_file():
        structure = read_json(root/'.govel/structure.json')
        fixture = {'structure': structure, 'chapterIds': [row['chapterId'] for row in structure['chapters']]}
    else:
        fixture = create_current_project(root, chapter_count=chapter_count)
    structure = fixture['structure']
    for row in structure['chapters']:
        row['status'] = 'planning_required'
    chapter = structure['chapters'][0]; cid = chapter['chapterId']
    (root/chapter['filePath']).unlink(missing_ok=True)
    chapter['proseBinding'] = seal_binding(structure['projectId'], cid, chapter['filePath'],
        state='unbound', generation=structure['revision'], expected_content_digest=None)
    write_json(root/'.govel/structure.json', structure)
    workflow = default_workflow(); workflow['approvedChapterIds'] = fixture['chapterIds']
    write_json(root/'.govel/workflow.json', workflow)
    register_and_approve(root, cid, 'stage_a_review', {
        'decision': 'adopt', 'activeSeats': list(range(1, 11)),
        'sourcePlanHash': chapter_plan(structure, cid)['sourcePlanHash'],
        'consensus': [], 'disagreements': [], 'openItems': []})
    advance_chapter_status(root, cid, 'planned')
    text = '他把钥匙放回桌上，合上了登记本。'
    result = pack_artifact(root, chapterId=cid, artifactType='candidate_prose', text=text,
                           advanceLifecycle=True, diagnostics={'mode': 'plot', 'intensityLevel': 1})
    if not result['ok']:
        raise AssertionError(result)
    workflow = read_json(root/'.govel/workflow.json')
    approve_artifacts(workflow, cid, artifact_hashes={kind: workflow['artifacts'][cid][kind]['artifactSha256']
        for kind in ('candidate_prose', 'diagnostics')}, approval_mode='manual', project_root=root)
    write_json(root/'.govel/workflow.json', workflow)
    for kind, payload in (
        ('stage_b_review', {'verdict': 'POLISH_READY', 'slots': evidenced_slots(text), 'findings': []}),
        ('state_candidate', {'changes': changes or []}),
    ):
        payload = assessed_review_payload(read_json(root/'.govel/structure.json'), cid, kind, payload)
        result = pack_artifact(root, chapterId=cid, artifactType=kind, payload=payload)
        workflow = read_json(root/'.govel/workflow.json')
        approve_artifacts(workflow, cid, artifact_hashes={kind: result['artifactSha256']}, approval_mode='manual', project_root=root)
        write_json(root/'.govel/workflow.json', workflow)
    return fixture
