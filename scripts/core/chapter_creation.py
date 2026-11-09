"""Create a chapter and caller-authored plan in one existing structure preview."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .artifacts import sha256_json
from .locks import project_lock
from .source_model import require_current_source
from .source_requests import request_identity, resume_request
from .structure_commands import _base_plan, _new_chapter, _write_plan_locked
from .structure_store import StructureError, load_structure


def preview_chapter(root: Path, *, requestId: str, title: str, primaryArcId: str, outlineText: str,
                    reason: str, volumeId: str | None = None, placement: dict | None = None,
                    beforeChapterId: str | None = None, secondaryArcIds: list[str] | None = None,
                    planField: str | None = None, dependsOn: list[str] | None = None,
                    chapterContract: dict | None = None, baseRevision: int | None = None) -> dict:
    from .source_placement import placement_from_volume
    if (placement is None) == (volumeId is None):
        raise StructureError('PLACEMENT_ARGUMENT_INVALID', 'provide exactly one of volumeId or placement')
    if not isinstance(outlineText, str) or not outlineText.strip() or not isinstance(reason, str) or not reason.strip():
        raise StructureError('CHAPTER_PLAN_REQUIRED', 'provide the actual chapter plan and reason')
    root = root.expanduser().resolve()
    arguments = {'title': title, 'primaryArcId': primaryArcId, 'outlineText': outlineText, 'reason': reason,
                 'placement': placement if placement is not None else placement_from_volume(volumeId),
                 'beforeChapterId': beforeChapterId, 'secondaryArcIds': secondaryArcIds or [],
                 'planField': planField, 'dependsOn': dependsOn or [], 'chapterContract': chapterContract,
                 'baseRevision': baseRevision}
    with project_lock(root):
        source = load_structure(root)
        require_current_source(source)
        operation_id, _ = request_identity(source, requestId)
        fingerprint = sha256_json({'operation': 'chapter-preview', 'arguments': arguments})
        prior = resume_request(root, source, operation_id, fingerprint)
        if prior is not None:
            return prior
        if baseRevision is not None and (type(baseRevision) is not int or baseRevision != source['revision']):
            raise StructureError('REVISION_CONFLICT', 'source changed before chapter creation')
        proposed, chapter_id, impact = _new_chapter(source, title=title, placement=arguments['placement'],
            before_chapter_id=beforeChapterId, primary_arc_id=primaryArcId, secondary_arc_ids=secondaryArcIds or [])
        parameters = {'chapterId': chapter_id, 'reason': reason, 'title': title,
                      'placement': arguments['placement'], 'primaryArcId': primaryArcId,
                      'secondaryArcIds': secondaryArcIds or [], 'beforeChapterId': beforeChapterId}
        from .source_changes import apply_changes
        fields = [row for row in source['fieldDefinitions']
                  if planField in {row['fieldId'], row['key']}
                  and row['valueType'] == 'topic' and 'chapter' in row['appliesTo']]
        if len(fields) != 1:
            available = [{key: row[key] for key in ('fieldId', 'key', 'label', 'valueType', 'appliesTo')}
                         for row in source['fieldDefinitions']
                         if row['valueType'] == 'topic' and 'chapter' in row['appliesTo']]
            recovery = {'nextOperation': 'source.schema', 'planField': planField,
                        'availablePlanFields': available, 'selectionRequired': True,
                        'schemaAction': {'operation': 'source.schema', 'arguments': {'projectRoot': str(root)}},
                        'retry': {'operation': 'workflow.chapter-preview', 'arguments': {
                            'projectRoot': str(root), 'requestId': requestId, **arguments},
                            'requiredInputs': ['planField']}}
            if not available:
                recovery['createFieldAction'] = {
                    'operation': 'source.change-preview',
                    'arguments': {'projectRoot': str(root), 'baseRevision': source['revision'],
                                  'changes': [{'action': 'put', 'collection': 'fieldDefinitions', 'record': {
                                      'fieldId': '$chapterPlanField', 'key': '<chosen.field.key>',
                                      'label': '<作者计划字段名称>', 'valueType': 'topic', 'appliesTo': ['chapter']}}],
                                  'reason': '<建立章节计划字段的理由>'},
                    'requiredInputs': ['requestId', 'reason', 'changes[0].record.key', 'changes[0].record.label'],
                    'requiresExplicitApproval': True,
                    'then': 'review preview, commit with its frozen operationId/planHash, retry original chapter request'}
            raise StructureError('CHAPTER_PLAN_FIELD_REQUIRED', 'select a registered chapter topic field by ID or key',
                                 recovery)
        fragment = {'fragmentId': '$plan', 'ownerId': chapter_id, 'order': 1,
                    'contentRole': 'plan', 'contentBindings': ['$planAttribute'],
                    'dependsOn': deepcopy(dependsOn or []), 'text': outlineText}
        if chapterContract is not None:
            fragment['chapterContract'] = deepcopy(chapterContract)
        proposed, identities = apply_changes(proposed, [
            {'action': 'put', 'collection': 'attributes', 'record': {
                'attributeId': '$planAttribute', 'ownerId': chapter_id, 'fieldId': fields[0]['fieldId'], 'values': []}},
            {'action': 'put', 'collection': 'fragments', 'record': fragment},
        ])
        parameters['identityMap'] = {'$chapter': chapter_id, **identities}
        plan = _base_plan(source, operation='insert_chapter', actor='app', parameters=parameters,
                          impact=impact, proposed=proposed)
        plan.update(operationId=operation_id, requestId=requestId, requestFingerprint=fingerprint)
        report = _write_plan_locked(root, plan)
        return report
