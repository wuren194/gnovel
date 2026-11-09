"""Registered dependency impact and durable, scoped semantic follow-up."""
from __future__ import annotations

from collections import defaultdict, deque
from functools import cached_property
from copy import deepcopy
from pathlib import Path

from .jsonio import read_json
from .source_model import SourceIndex, author_content
from .structure_store import StructureError, canonical_json, structure_hash
from .write_sets import digest, json_bytes, safe_project_path

REVIEW_PATH = '.gnovel/source-review.json'


def time_constraint_content(record):
    """Only temporal declarations can constrain a referenced time point."""
    if not isinstance(record, dict):
        return record
    return {key: record[key] for key in ('chapterId', 'entityId', 'entityType',
        'storyTimeStartId', 'storyTimeEndId', 'timeStartId', 'timeEndId', 'timeSpec', 'timeOrdinal') if key in record}


def dependency_edges(source: dict) -> list[tuple[str, str, str]]:
    index = SourceIndex.build(source)
    edges = []
    from .pair_classification import pair_inputs
    chapter_pairs = {row['chapterId']: pair_inputs(source, row['chapterId'])['pairs']
                     for row in source.get('chapters', [])}
    for pair in source.get('pairs', []):
        if pair.get('status', 'active') != 'archived':
            edges.extend((cid, pair['pairId'], 'pair_character') for cid in pair['characterIds'])
    for cid, pairs in chapter_pairs.items():
        edges.extend((pair['pairId'], cid, 'pair_applicability') for pair in pairs)
    # Classification is a real input of planning, including a missing assessment.
    # This also makes existing semantic fragment receipts stale when it changes.
    for fid, fragment in index.fragments.items():
        if fragment.get('contentRole') == 'plan' and fragment.get('status') != 'archived':
            edges.append((source['projectId'], fid, 'pair_assessment'))
            pairs = chapter_pairs.get(fragment['ownerId'])
            if pairs is None:
                pairs = pair_inputs(source, scope_ids={source['projectId'], fragment['ownerId']})['pairs']
            edges.extend((pair['pairId'], fid, 'pair_applicability') for pair in pairs)
    for aid, attribute in index.attributes.items():
        edges.append((attribute['fieldId'], aid, 'field_definition'))
        for value in attribute['values']:
            # A direct value dependency still uses its field's meaning, even
            # when the consuming fragment is bound to another attribute.
            edges.append((attribute['fieldId'], value['valueId'], 'field_definition'))
            edges.append((value['valueId'], aid, 'attribute_value'))
            if (index.fields[attribute['fieldId']]['valueType'] == 'reference'
                    and value.get('state', 'known') == 'known'):
                edges.append((value['value'], value['valueId'], 'reference_value'))
    for fid, fragment in index.fragments.items():
        edges.extend((target, fid, 'depends_on') for target in fragment.get('dependsOn', []))
        edges.extend((target, fid, 'content_binding') for target in fragment['contentBindings'])
    for reference in source.get('references', []):
        edges.append((reference['targetId'], reference['sourceId'], 'chapter_reference'))
    for collection, identity, fields in (
            ('chapters', 'chapterId', ('storyTimeStartId', 'storyTimeEndId')),
            ('entities', 'entityId', ('timeStartId', 'timeEndId'))):
        for row in source.get(collection, []):
            edges.extend((row[field], row[identity], 'time_mount') for field in fields if row.get(field))
            if all(row.get(field) for field in fields):
                # start <= end can constrain either endpoint. The declaration
                # owner is part of the same input closure; a numeric origin is not.
                edges.extend((row[identity], row[field], 'time_constraint') for field in fields)
    for row in source.get('entities', []):
        if row['entityType'] in {'event', 'time_anchor'}:
            spec = row.get('timeSpec') or {}
            # Both range bounds are inputs, regardless of their chronological order.
            edges.extend((spec[field], row['entityId'], 'time_spec')
                         for field in ('anchorId', 'afterId', 'beforeId') if spec.get(field))
            # Relative equality and strict order can narrow referenced points
            # as well as the declaring point. Keep every contributing declaration
            # in the existing plan/review fingerprint rather than a second graph.
            edges.extend((row['entityId'], spec[field], 'time_constraint')
                         for field in ('anchorId', 'afterId', 'beforeId') if spec.get(field))
    for relation in source.get('relations', []):
        for endpoint in ('fromEntity', 'toEntity'):
            edges.append((relation['relationId'], relation[endpoint], 'relation_endpoint'))
    for record_id, record in index.records.items():
        period = record.get('validDuring') or {}
        for anchor in period.values():
            if anchor is not None:
                edges.append((anchor, record_id, 'valid_during'))
    for row in source.get('presentations', []):
        edges.append((row['eventId'], row['chapterId'], 'event_presentation'))
    from .source_rules import rule_inputs
    for row in source.get('entities', []):
        if row.get('ruleSpec'):
            edges.extend((rid, row['entityId'], 'rule_input') for rid in rule_inputs(row))
    return edges


def _content_view(record_id: str, row: dict | None, owner_type: str | None) -> object:
    """Authored-content view of one record for change detection.

    A chapter's `filePath` is ownership locator metadata, not story content: a pure
    locator move A → B must never be misread as a canon/story-fact/planning change
    (实施轮 §31–§33), so it produces no staleCandidates and no approvalInvalidations
    by itself. `proseBinding` (generation/digest reseal) and the lifecycle fields
    (`status`/`outlinePath`) are excluded on the same grounds and with the same list
    `chapter_plan()` already projects away — one authored-content projection, not two.
    """
    return author_content(row, owner_type)


def identity_changes(before: dict, after: dict, *,
                     indexes: tuple[SourceIndex, SourceIndex] | None = None) -> dict[str, list[str]]:
    """Pure identity projection shared by preview impact and committed ledger checks."""
    old, new = indexes or (SourceIndex.build(before), SourceIndex.build(after))
    changed = sorted(rid for rid in old.records.keys() | new.records.keys()
                     if canonical_json(_content_view(rid, old.records.get(rid), old.owner_types.get(rid)))
                     != canonical_json(_content_view(rid, new.records.get(rid), new.owner_types.get(rid))))
    old_candidates = {row['candidateId']: row for row in before.get('planningCandidates', [])}
    new_candidates = {row['candidateId']: row for row in after.get('planningCandidates', [])}
    candidate_changes = sorted(cid for cid in old_candidates.keys() | new_candidates.keys()
                               if old_candidates.get(cid) != new_candidates.get(cid))
    old_decisions = {row['decisionId']: row for row in before.get('authorDecisions', [])}
    new_decisions = {row['decisionId']: row for row in after.get('authorDecisions', [])}
    decision_changes = sorted(did for did in old_decisions.keys() | new_decisions.keys()
                              if old_decisions.get(did) != new_decisions.get(did))
    return {'changedIds': changed, 'planningCandidateChanges': candidate_changes,
            'authorDecisionChanges': decision_changes,
            'workVoiceProfileChanged': before.get('workVoiceProfile') != after.get('workVoiceProfile')}


def source_impact(before: dict, after: dict, *, root: Path | None = None) -> dict:
    old, new = SourceIndex.build(before), SourceIndex.build(after)
    identities = identity_changes(before, after, indexes=(old, new))
    changed = identities['changedIds']
    relation_changes = []
    for rid in changed:
        prior = old.records.get(rid)
        current = new.records.get(rid)
        if not any(isinstance(row, dict) and 'relationId' in row for row in (prior, current)):
            continue
        relation_changes.append({'relationId': rid,
                                 'before': {k: prior.get(k) for k in ('fromEntity', 'toEntity', 'kind')} if prior else None,
                                 'after': {k: current.get(k) for k in ('fromEntity', 'toEntity', 'kind')} if current else None})
    reverse = defaultdict(set)
    for origin, target, kind in dependency_edges(before) + dependency_edges(after):
        reverse[origin].add((target, kind))
    paths = {rid: None for rid in changed}
    causes = {rid: rid for rid in changed}
    queue = deque(changed)
    while queue:
        origin = queue.popleft()
        for target, kind in sorted(reverse[origin]):
            if (kind == 'time_constraint' and origin in changed
                    and time_constraint_content(old.records.get(origin)) == time_constraint_content(new.records.get(origin))):
                continue
            if target in paths:
                continue
            paths[target] = {'fromId': origin, 'toId': target, 'kind': kind}
            causes[target] = causes[origin]
            queue.append(target)
    affected_fragments = sorted(rid for rid in paths if rid in new.fragments and new.fragments[rid].get('status') != 'archived')
    chapters = {rid for rid in paths if new.owner_types.get(rid) == 'chapter'}
    chapters.update(new.fragments[rid]['ownerId'] for rid in affected_fragments
                    if new.owner_types.get(new.fragments[rid]['ownerId']) == 'chapter')
    from .source_plan import chapter_plan
    old_inputs, new_inputs = ReviewInputs(before), ReviewInputs(after)
    for cid, kind in new.owner_types.items():
        if kind != 'chapter' or old.owner_types.get(cid) != 'chapter':
            continue
        old_plan = chapter_plan(before, cid, _inputs=old_inputs)
        new_plan = chapter_plan(after, cid, _inputs=new_inputs)
        if old_plan['sourcePlanHash'] != new_plan['sourcePlanHash']:
            chapters.add(cid)
    prose = []
    if root is not None:
        from .source_evidence import prose_impact
        prose, prose_chapters = prose_impact(root, after, set(paths))
        chapters.update(prose_chapters)
    from .source_rules import evaluate_rule
    rule_checks = [evaluate_rule(new.objects[rid], new_inputs) for rid in sorted(paths)
                   if rid in new.objects and new.objects[rid].get('ruleSpec')
                   and new.objects[rid].get('status') != 'archived']
    # Impact results describe checks, not whether a particular action is blocked.
    for row in rule_checks:
        row.pop('blocking', None)
    return {**identities,
            'relationChanges': relation_changes, 'ruleChecks': rule_checks,
            'registeredDependents': [{'recordId': rid, 'via': paths[rid], 'causeId': causes[rid]}
                                     for rid in sorted(paths) if paths[rid]],
            'reviewFragmentIds': affected_fragments, 'proseEvidence': prose,
            'staleCandidates': sorted(chapters), 'approvalInvalidations': sorted(chapters),
            'coverage': {'registeredDependencyClosureComplete': True, 'proseScanned': False,
                         'semanticReviewComplete': False,
                         'absenceMeaning': 'no registered dependency found; unregistered and implicit usage is not ruled out'},
            'proseWrite': False, 'readingExport': False}


class ReviewInputs:
    """Reuse one reverse index per snapshot, including cyclic dependencies."""
    def __init__(self, source: dict):
        self.source = source
        self.index = SourceIndex.build(source)
        self.rules = [row for row in source['entities'] if row.get('entityType') == 'rule']
        self.incoming = defaultdict(set)
        self.incoming_edges = defaultdict(set)
        for origin, target, kind in dependency_edges(source):
            self.incoming[target].add(origin)
            self.incoming_edges[target].add((origin, kind))

    @cached_property
    def source_hash(self) -> str:
        from .source_model import snapshot_hash
        return snapshot_hash(self.source)

    def closure(self, roots, *, include_owners=False):
        """Return full or temporal inputs, upgrading when an explicit path exists.

        A reverse time constraint consumes a declaration's endpoints, not its
        title, prose locator, personality or unrelated planning dependencies.
        """
        found, queue = {}, [(rid, False) for rid in roots]
        while queue:
            rid, temporal = queue.pop()
            if rid in found and (found[rid] is False or temporal):
                continue
            found[rid] = temporal
            for origin, kind in self.incoming_edges[rid]:
                if temporal and kind not in {'time_constraint', 'time_mount', 'time_spec'}:
                    continue
                queue.append((origin, temporal or kind == 'time_constraint'))
            if include_owners and not temporal:
                owner = self.index.owner(rid)
                if owner:
                    queue.append((owner, False))
        return found

    def content(self, record_id, temporal, *, authored=False):
        row = self.index.records.get(record_id)
        if temporal:
            return time_constraint_content(row)
        return author_content(row, self.index.owner_types.get(record_id)) if authored else row

    def fingerprint(self, fragment_id: str) -> str:
        found = self.closure([fragment_id])
        return digest(canonical_json({rid: self.content(rid, found[rid]) for rid in sorted(found)}).encode())


def review_input_hash(source: dict, fragment_id: str) -> str:
    return ReviewInputs(source).fingerprint(fragment_id)


def read_review(root: Path, *, source: dict | None = None) -> dict:
    path = safe_project_path(root, REVIEW_PATH)
    state = read_json(path) if path.exists() else {'sourceHash': None, 'items': {}}
    if (not isinstance(state, dict) or not isinstance(state.get('items'), dict)
            or any(not isinstance(item, dict) or item.get('recordId') != rid or not isinstance(item.get('inputHash'), str)
                   or not isinstance(item.get('status'), str) or item['status'] not in {
                       'pending', 'compatible', 'resolved', 'obsolete', 'conflict', 'needs_information', 'not_checked'}
                   for rid, item in state.get('items', {}).items())):
        raise StructureError('SOURCE_REVIEW_INVALID', 'source review state is unreadable')
    from .source_review import _receipts, review_matches_current, review_state_item
    receipts = _receipts(root)['receipts']
    checked = [item for item in state['items'].values() if item['status'] in {'compatible', 'resolved', 'conflict', 'needs_information', 'not_checked'}]
    if checked:
        for item in checked:
            receipt_id = item.get('receiptId')
            receipt = receipts.get(receipt_id) if isinstance(receipt_id, str) else None
            if receipt is None or not any(row['recordId'] == item['recordId'] and row['inputHash'] == item['inputHash']
                                          and row['decision'] == item['status'] for row in receipt['reviews']):
                raise StructureError('SOURCE_REVIEW_INVALID', 'review state has no matching semantic receipt')
    if not receipts:
        return state
    if source is None:
        from .structure_store import load_structure
        source = load_structure(root)
    inputs, selected, hashes = ReviewInputs(source), set(), {}
    # Receipt insertion order is preserved by the atomic JSON writer. Take the
    # latest judgment for these exact inputs, including conflict/not_checked;
    # never search past a newer failed judgment for an older compatible one.
    for receipt in reversed(list(receipts.values())):
        for row in receipt['reviews']:
            rid = row['recordId']
            fragment = inputs.index.fragments.get(rid)
            if rid in selected or fragment is None or fragment.get('status') == 'archived':
                continue
            if rid not in hashes:
                hashes[rid] = inputs.fingerprint(rid)
            if row['inputHash'] != hashes[rid]:
                continue
            selected.add(rid)
            if not review_matches_current(inputs, row, input_hash=hashes[rid]):
                state['items'][rid] = {'recordId': rid, 'ownerId': fragment['ownerId'],
                    'status': 'pending', 'inputHash': hashes[rid], 'revision': source['revision']}
                continue
            prior = state['items'].get(rid, {})
            if (prior.get('receiptId') == receipt['receiptId'] and prior.get('inputHash') == hashes[rid]
                    and prior.get('status') == row['decision']):
                continue
            state['items'][rid] = review_state_item(
                {'revision': receipt.get('revision', source['revision'])}, row, receipt['receiptId'], fragment)
    return state


def fragment_review_status(inputs: ReviewInputs, state: dict, record_id: str, *, input_hash: str | None = None) -> str:
    """One read-side status calculation for queries, exports and maintenance checks."""
    fragment = inputs.index.fragments.get(record_id)
    if fragment is None or fragment.get('status') == 'archived':
        return 'missing_or_archived'
    prior = state['items'].get(record_id)
    if prior is None:
        return 'unreviewed'
    current_hash = input_hash if input_hash is not None else inputs.fingerprint(record_id)
    return 'stale' if prior['inputHash'] != current_hash else prior['status']


def review_outputs(root: Path, before: dict, after: dict, impact: dict) -> dict[str, bytes]:
    state = deepcopy(read_review(root, source=after))
    current = SourceIndex.build(after)
    inputs = ReviewInputs(after)
    targets = set(impact['reviewFragmentIds'])
    for rid, item in state['items'].items():
        if rid not in current.fragments or current.fragments[rid].get('status') == 'archived':
            item['status'] = 'obsolete'
        elif item.get('inputHash') != inputs.fingerprint(rid):
            targets.add(rid)
    for rid in sorted(targets):
        input_hash = inputs.fingerprint(rid)
        prior = state['items'].get(rid, {})
        if prior.get('inputHash') == input_hash and prior.get('status') in {
                'compatible', 'resolved', 'conflict', 'needs_information', 'not_checked'}:
            continue
        state['items'][rid] = {'recordId': rid, 'ownerId': current.fragments[rid]['ownerId'],
                               'status': 'pending', 'inputHash': input_hash,
                               'operationId': (after.get('lastOperation') or {}).get('operationId'),
                               'revision': after['revision']}
    state['sourceHash'] = structure_hash(after)
    outputs = {REVIEW_PATH: json_bytes(state)}
    path = safe_project_path(root, '.gnovel/workflow.json')
    if path.is_file() and impact['approvalInvalidations']:
        workflow = read_json(path)
        for chapter_id in impact['approvalInvalidations']:
            approval = workflow.get('approvals', {}).get(chapter_id)
            if approval is not None:
                approval['approved'] = False
                approval['invalidatedBy'] = 'source_dependencies_changed'
            for record in workflow.get('artifacts', {}).get(chapter_id, {}).values():
                if isinstance(record, dict):
                    record['invalidatedBy'] = 'source_dependencies_changed'
                    history = record.get('repairHistory', [])
                    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
                        raise StructureError('REPAIR_HISTORY_INVALID', 'registered repair history is unreadable')
                    # All retained versions predate this source change, including
                    # reviews replaced before the current version was invalidated.
                    for reference in history:
                        reference['invalidatedBy'] = 'source_dependencies_changed'
        outputs['.gnovel/workflow.json'] = json_bytes(workflow)
    return outputs
