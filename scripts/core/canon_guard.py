"""Final single-source Canon mutation boundary, shared by preview and commit.

All project mutations use the current single-source model.
"""
from __future__ import annotations

from .structure_store import StructureError


def _rows(source: dict, collection: str, key: str) -> dict:
    return {row[key]: row for row in source.get(collection, [])}


class _EntityRefactor:
    """Display-field refactor exemption for entities.

    `refactor_rename_entity` / `refactor_alias_entity` / `refactor_archive_entity`
    / `refactor_restore_entity` are authoring refactors, not canon proposals: a
    rename is display metadata and an archive is a lifecycle flip, neither of
    which needs fact-quote evidence. The exemption is narrow on purpose — only
    `name`, `aliases` and `status` may differ, no other canon collection may be
    touched, and an entity may never be deleted through a refactor.
    """

    OPERATIONS = {'refactor_rename_entity', 'refactor_alias_entity',
                  'refactor_archive_entity', 'refactor_restore_entity'}
    FIELDS = {'name', 'aliases', 'status'}


def _require_entity_display_refactor(before: dict, proposed: dict, changes: dict) -> None:
    others = changes['fragments'] | changes['attributes'] | changes['relations'] | changes['fieldDefinitions']
    if others:
        raise StructureError('CANON_PROPOSAL_REQUIRED', 'refactor touched non-display canon records',
                             {'ids': sorted(others)})
    after = _rows(proposed, 'entities', 'entityId')
    for rid in changes['entities']:
        old_row, new_row = before['entities'].get(rid), after.get(rid)
        if new_row is None:
            raise StructureError('CANON_PROPOSAL_REQUIRED', 'refactor cannot delete an entity',
                                 {'collection': 'entities', 'ids': [rid]})
        diff = {key for key in set(old_row) | set(new_row) if old_row.get(key) != new_row.get(key)}
        unexpected = diff - _EntityRefactor.FIELDS
        if unexpected:
            raise StructureError('CANON_PROPOSAL_REQUIRED',
                                 'entity refactor may only change name, aliases or status',
                                 {'collection': 'entities', 'ids': [rid], 'fields': sorted(unexpected)})


def require_canon_proposal(current: dict, proposed: dict, plan: dict) -> None:
    from .source_model import require_current_source
    require_current_source(current)
    require_current_source(proposed)
    from .author_decisions import canon_proposal_content
    proposed = canon_proposal_content(current, proposed, plan)
    from .source_cognition import require_check_transition
    require_check_transition(current, proposed, plan)
    operation = plan['operation']
    if operation == 'canon_fact_patch':
        from .source_fact_patch import require_fact_patch
        require_fact_patch(current, proposed, plan)
        return
    if operation == 'canon_proposal_batch':
        from .source_proposal_batch import require_proposal_batch
        require_proposal_batch(current, proposed, plan)
        return
    if operation == 'canon_field_definition_proposal':
        from .source_field_proposals import require_field_definition_proposal
        require_field_definition_proposal(current, proposed, plan)
        return
    if operation == 'canon_field_migration_proposal':
        from .source_field_migrations import require_field_migration_proposal
        require_field_migration_proposal(current, proposed, plan)
        return
    changes = {}
    for name, key in (('entities', 'entityId'), ('attributes', 'attributeId'),
                      ('fragments', 'fragmentId'), ('relations', 'relationId'),
                      ('fieldDefinitions', 'fieldId')):
        before, after = _rows(current, name, key), _rows(proposed, name, key)
        changes[name] = {rid for rid in before.keys() | after.keys() if before.get(rid) != after.get(rid)}
        if name in {'entities', 'attributes', 'fragments', 'relations'}:
            removed = set(before) - set(after)
            if name == 'entities' and removed:
                raise StructureError('CANON_PROPOSAL_REQUIRED', 'existing entity hard delete is unavailable')
    before = {name: _rows(current, name, key) for name, key in
              (('entities', 'entityId'), ('attributes', 'attributeId'), ('fragments', 'fragmentId'),
               ('relations', 'relationId'), ('fieldDefinitions', 'fieldId'))}
    existing = {name: changes[name] & set(before[name]) for name in before}
    referenced = {row['fieldId'] for row in current.get('attributes', [])}
    if existing['fieldDefinitions'] & referenced:
        raise StructureError('FIELD_DEFINITION_MIGRATION_REQUIRED',
                             'referenced field definitions require a reviewed semantic migration',
                             {'fieldIds': sorted(existing['fieldDefinitions'] & referenced)})
    if operation in _EntityRefactor.OPERATIONS:
        _require_entity_display_refactor(before, proposed, changes)
        return
    protected_facts = {rid for rid in existing['fragments']
                       if before['fragments'][rid].get('contentRole') == 'fact'}
    promoted_facts = {rid for rid in existing['fragments']
                      if before['fragments'][rid].get('contentRole') != 'fact' and
                      _rows(proposed, 'fragments', 'fragmentId').get(rid, {}).get('contentRole') == 'fact'}
    if promoted_facts:
        raise StructureError('FACT_PROMOTION_REVIEW_REQUIRED',
                             'an existing non-fact fragment cannot be silently promoted to Canon',
                             {'fragmentIds': sorted(promoted_facts)})
    protected = {'entities': existing['entities'], 'attributes': existing['attributes'],
                 'fragments': protected_facts, 'relations': existing['relations']}
    expected_operation = {'entities': 'canon_change_proposal',
                          'attributes': 'canon_content_proposal', 'fragments': 'canon_content_proposal',
                          'relations': 'canon_relation_proposal'}
    error_code = {'entities': 'CANON_PROPOSAL_REQUIRED', 'attributes': 'CONTENT_PROPOSAL_REQUIRED',
                  'fragments': 'CONTENT_PROPOSAL_REQUIRED', 'relations': 'RELATION_PROPOSAL_REQUIRED'}
    for name, ids in protected.items():
        if ids and operation != expected_operation[name]:
            raise StructureError(error_code[name], 'existing Canon record requires a reviewed proposal',
                                 {'collection': name, 'ids': sorted(ids)})
    proposal_kind = {'canon_change_proposal': ('entities', 'canonProposal'),
                     'canon_content_proposal': (None, 'contentProposal'),
                     'canon_relation_proposal': ('relations', 'relationProposal')}
    if operation not in proposal_kind:
        return
    target_collection, parameter = proposal_kind[operation]
    proposal = plan.get('parameters', {}).get(parameter)
    scope_error = {'canon_change_proposal': 'CANON_PROPOSAL_SCOPE_INVALID',
                   'canon_content_proposal': 'CONTENT_PROPOSAL_SCOPE_INVALID',
                   'canon_relation_proposal': 'RELATION_PROPOSAL_SCOPE_INVALID'}[operation]
    if not isinstance(proposal, dict):
        raise StructureError(scope_error, 'proposal metadata is required')
    if operation == 'canon_change_proposal':
        rid = proposal.get('entityId')
        allowed = {'entities': {rid}}
        old, new = before['entities'].get(rid), _rows(proposed, 'entities', 'entityId').get(rid)
        path = proposal.get('fieldPath')
        from .canon_proposals import MUTABLE_FIELDS
        if not isinstance(path, str) or path not in MUTABLE_FIELDS and not (path.startswith('fields.')
                                                and path.count('.') == 1 and len(path) > 7):
            raise StructureError(scope_error, 'entity proposal uses an unsupported field path')
        if not isinstance(old, dict) or not isinstance(new, dict) or not isinstance(path, str):
            raise StructureError(scope_error, 'entity proposal must retain its target')
        expected = dict(old)
        if path.startswith('fields.') and path.count('.') == 1:
            fields = dict(expected.get('fields') or {})
            key = path[7:]
            actual_exists, actual = key in fields, fields.get(key)
            if proposal.get('newValue') is None:
                fields.pop(key, None)
            else:
                fields[key] = proposal['newValue']
            if fields:
                expected['fields'] = fields
            else:
                expected.pop('fields', None)
        else:
            actual_exists, actual = path in expected, expected.get(path)
            if proposal.get('newValue') is None:
                expected.pop(path, None)
            else:
                expected[path] = proposal['newValue']
        if actual_exists != proposal.get('expectedOldExists') or actual != proposal.get('expectedOldValue') or expected != new:
            raise StructureError(scope_error, 'entity proposal does not match its exact field diff')
        fragments = _rows(current, 'fragments', 'fragmentId')
        evidence = proposal.get('evidence')
        if not isinstance(evidence, list) or not evidence or not any(
                isinstance(item, dict) and set(item) == {'fragmentId', 'quote'} and
                isinstance(item['quote'], str) and item['quote'].strip() and
                item['fragmentId'] in fragments and
                fragments[item['fragmentId']].get('contentRole') == 'fact' and
                fragments[item['fragmentId']].get('status') != 'archived' and
                item['quote'] in fragments[item['fragmentId']]['text'] and
                fragments[item['fragmentId']]['ownerId'] in {rid, proposal.get('sourceChapterId')}
                for item in evidence):
            raise StructureError(scope_error, 'entity proposal has no relevant current fact quote')
    elif operation == 'canon_content_proposal':
        kind, rid = proposal.get('kind'), proposal.get('recordId')
        target_collection = {'attribute': 'attributes', 'fact': 'fragments'}.get(kind)
        next_record = (_rows(proposed, target_collection,
                             'attributeId' if kind == 'attribute' else 'fragmentId').get(rid)
                       if target_collection else None)
        if target_collection is None or rid not in before[target_collection] or \
                proposal.get('expectedOldRecordHash') != _fingerprint(before[target_collection][rid]) or \
                proposal.get('newRecordHash') != _fingerprint(next_record):
            raise StructureError(scope_error, 'content proposal does not match current and proposed record')
        old = before[target_collection][rid]
        if next_record is not None and (next_record.get('ownerId') != old['ownerId'] or
                                        next_record.get('attributeId' if kind == 'attribute' else 'fragmentId') != rid or
                                        kind == 'fact' and next_record.get('contentRole') != 'fact'):
            raise StructureError(scope_error, 'content proposal changed identity, owner or fact role')
        from .source_content_proposals import _validate_evidence
        _validate_evidence(current, kind, old, proposal.get('sourceChapterId'), proposal.get('evidence'))
        allowed = {target_collection: {rid}}
    else:
        rid = proposal.get('relationId')
        old = before['relations'].get(rid)
        new = _rows(proposed, 'relations', 'relationId').get(rid)
        if old is None or proposal.get('expectedOldRecord') != old or proposal.get('newRecord') != new:
            raise StructureError(scope_error, 'relation proposal does not match its exact old/new records')
        from .source_relation_proposals import MUTABLE_FIELDS
        if new is not None and ({key: value for key, value in new.items() if key not in MUTABLE_FIELDS} !=
                                {key: value for key, value in old.items() if key not in MUTABLE_FIELDS}):
            raise StructureError(scope_error, 'relation identity, direction and provenance are immutable')
        from .source_relation_proposals import _resolved, _validate_evidence
        resolved, reference = _resolved(current, proposal.get('sessionRef'))
        if resolved != old or reference['chapterId'] != proposal.get('sourceChapterId'):
            raise StructureError(scope_error, 'relation session does not match proposal target')
        _validate_evidence(current, old, proposal.get('sourceChapterId'),
                           proposal.get('evidenceMode'), proposal.get('evidence'))
        allowed = {'relations': {rid}}
    for name, ids in changes.items():
        if ids != allowed.get(name, set()):
            raise StructureError(scope_error, 'proposal changed another source record',
                                 {'collection': name, 'ids': sorted(ids)})
    for name, key in (('entities', 'entityId'), ('attributes', 'attributeId'),
                      ('fragments', 'fragmentId'), ('relations', 'relationId'),
                      ('fieldDefinitions', 'fieldId')):
        expected_rows = list(current.get(name, []))
        if name in allowed:
            target_id = next(iter(allowed[name]))
            next_row = _rows(proposed, name, key).get(target_id)
            expected_rows = [next_row if row[key] == target_id else row for row in expected_rows
                             if row[key] != target_id or next_row is not None]
        if expected_rows != proposed.get(name, []):
            raise StructureError(scope_error, 'proposal changed source row order or another record',
                                 {'collection': name})
    ignored = set(changes) | {'revision', 'updatedAt', 'lastOperation'}
    if any(current.get(key) != proposed.get(key) for key in set(current) | set(proposed) if key not in ignored):
        raise StructureError(scope_error, 'proposal changed unrelated source state')
    approval = proposal.get('approval')
    if not isinstance(approval, dict) or not approval.get('actorId') or not approval.get('evidence') or \
            approval.get('verifiedHuman') is not False or not proposal.get('reason'):
        raise StructureError(scope_error, 'proposal lacks declared approval and reason')


def _fingerprint(value) -> str:
    from .source_review import fingerprint
    return fingerprint(value)
