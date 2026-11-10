"""Explicit chapter facts routed through source plans and existing prose receipts.

The caller supplies every statement, owner, topic field, dependency and quotation.
This operation declares that each selected quote depends on the corresponding fact;
it never extracts facts, reviews their meaning, or approves a source transaction.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .consistency import require_no_pending
from .continuity import validated_continuity_record
from .evidence_quotes import quote_span
from .locks import project_lock_reentrant
from .project import canon_proposal_batch_max_items, query_policy
from .prose_binding import OWNED_CLEAN, require_observation
from .source_model import SourceIndex, require_current_source, snapshot_hash
from .source_requests import request_identity, request_state
from .structure_store import StructureError, load_structure
from .write_sets import guard_observation, safe_project_path, validate_guards


def _action(operation, **arguments):
    return {'operation': operation, 'arguments': arguments}


def _bounded_report(root, report):
    """Keep executable actions visible; large audit details use the existing reader."""
    from .query import _size
    if _size(report) <= query_policy()['maxBytes']['detail']:
        return report
    from .transport_cache import put
    reference = put(root, 'authoring-report', report)
    compact = {key: value for key, value in report.items()
               if key not in {'sourcePreview', 'evidenceSubmission', 'facts', 'blockers'}}
    compact.update({'facts': [], 'factsIncluded': False,
                    'blockers': [{'code': row['code']} for row in report['blockers']],
                    'responseCompacted': True, 'reportRef': reference,
                    'detailsAction': _action('task.snapshot', reference=reference)})
    if _size(compact) > query_policy()['maxBytes']['detail']:
        raise StructureError('CHAPTER_FACTS_BUDGET_INVALID', 'request metadata exceeds the response budget')
    return compact


def _formal_chapter(root, source, chapter_id):
    chapter = next((row for row in source['chapters'] if row['chapterId'] == chapter_id), None)
    if chapter is None or chapter.get('status') != 'stable':
        raise StructureError('CHAPTER_FACTS_STAGE_REQUIRED',
            'new chapter facts require committed prose and completed continuity',
            {'chapterId': chapter_id, 'requiredStatus': 'stable',
             'nextAction': _action('project.workflow.chapter-receipt', chapterId=chapter_id)})
    workflow_path = '.govel/workflow.json'
    workflow_state, raw = guard_observation(safe_project_path(root, workflow_path))
    try:
        workflow = json.loads(raw) if raw is not None else {}
        record = validated_continuity_record(workflow, chapter_id)
    except (TypeError, ValueError) as exc:
        raise StructureError('CHAPTER_FACTS_CONTINUITY_REQUIRED', 'continuity receipt is unreadable',
                             {'chapterId': chapter_id}) from exc
    imported_proof = None
    if record is None:
        from .import_history import history_entry, require_history
        if history_entry(workflow, chapter_id) is not None:
            imported_proof = require_history(root, source, workflow, chapter)
    if imported_proof is None and (record is None or record['status'] != 'complete'):
        raise StructureError('CHAPTER_FACTS_CONTINUITY_REQUIRED',
            'complete this chapter continuity before previewing source facts; source commits change its pending snapshot',
            {'chapterId': chapter_id, 'nextAction': _action(
                'project.workflow.continuity-preview', chapterId=chapter_id),
             'automaticConfirmationAllowed': False})
    require_observation(root, source, chapter, (OWNED_CLEAN,), action='chapter fact evidence')
    path = chapter['filePath']
    observed, prose = guard_observation(safe_project_path(root, path))
    if prose is None or 'sha256:' + str(observed['sha256']) != chapter['proseBinding']['expectedContentDigest']:
        raise StructureError('CHAPTER_FACTS_PROSE_CHANGED', 'formal prose changed during evidence preparation')
    try:
        text = prose.decode('utf-8')
    except UnicodeError as exc:
        raise StructureError('SOURCE_EVIDENCE_ENCODING', 'chapter manuscript must be UTF-8') from exc
    guards = [{'path': path, **observed}, {'path': workflow_path, **workflow_state}]
    validate_guards(root, guards, stage='chapter-facts-read')
    return ({'path': path, 'sha256': observed['sha256'],
             'bindingGeneration': chapter['proseBinding']['generation']},
            ({'batchHash': imported_proof['reviewHash'], 'confirmationHash': imported_proof['reviewHash'],
              'importOperationId': imported_proof['operationId']} if imported_proof else
             {'batchHash': record['batchHash'], 'confirmationHash': record['confirmationHash']}), text, guards)


def _assemble(root, source, chapter_id, facts):
    if (not isinstance(chapter_id, str) or not isinstance(facts, list) or not facts
            or len(facts) > canon_proposal_batch_max_items()):
        raise StructureError('CHAPTER_FACTS_INPUT_INVALID', 'chapterId and a bounded nonempty facts list are required')
    manuscript, continuity, text, guards = _formal_chapter(root, source, chapter_id)
    index = SourceIndex.build(source)
    attributes = {(row['ownerId'], row['fieldId']): row['attributeId'] for row in source['attributes']}
    orders = {owner: max((row['order'] for row in source['fragments'] if row['ownerId'] == owner), default=0)
              for owner in index.objects}
    changes, selected, seen = [], [], set()
    for number, fact in enumerate(facts, 1):
        required = {'ownerId', 'field', 'statement', 'quote', 'reason'}
        if (not isinstance(fact, dict) or not required <= fact.keys() or set(fact) - required - {'dependsOn', 'findingId'}
                or any(not isinstance(fact[key], str) or not fact[key].strip()
                       for key in ('ownerId', 'field', 'statement', 'reason'))):
            raise StructureError('CHAPTER_FACTS_INPUT_INVALID', 'each fact needs an explicit owner, field, statement, quote and reason')
        owner = index.objects.get(fact['ownerId'])
        field = next((row for row in index.fields.values()
                      if fact['field'] in (row['fieldId'], row['key'])), None)
        if owner is None or owner.get('status') == 'archived':
            raise StructureError('CHAPTER_FACTS_OWNER_INVALID', 'select an active existing owner', {'ownerId': fact['ownerId']})
        if field is None or index.owner_types[fact['ownerId']] not in field['appliesTo']:
            raise StructureError('CHAPTER_FACTS_FIELD_INVALID', 'select an existing field applicable to this owner',
                                 {'field': fact['field'], 'nextAction': _action('source.schema')})
        if field['valueType'] != 'topic':
            raise StructureError('CHAPTER_FACTS_TOPIC_REQUIRED',
                'statement-only facts require an existing topic field; typed values use the explicit source proposal tools',
                {'fieldId': field['fieldId'], 'valueType': field['valueType'], 'nextAction': _action('source.schema')})
        dependencies = fact.get('dependsOn', [])
        if (not isinstance(dependencies, list) or any(not isinstance(rid, str) or rid not in index.records
                or index.records[rid].get('status') == 'archived' for rid in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise StructureError('CHAPTER_FACTS_DEPENDENCY_INVALID', 'dependsOn must name distinct existing active records')
        quoted = quote_span(text, {'text': fact['quote']} if isinstance(fact['quote'], str) else fact['quote'])
        if 'findingId' in fact:
            from .fact_inventory import retained_findings
            from .finalization import _load_workflow
            findings, unavailable = retained_findings(root, source, _load_workflow(root), chapter_id)
            finding = findings.get(fact['findingId'], {})
            change = finding.get('factChange', {})
            expected_quotes = [quote_span(text, q) for q in change.get('evidence', [])]
            if unavailable or change.get('status') != 'pending' or quoted not in expected_quotes:
                raise StructureError('CHAPTER_FACTS_FINDING_INVALID', 'findingId must select a retained pending B fact and its reviewed quote')
        slot = (fact['ownerId'], field['fieldId'])
        identity = (*slot, fact['statement'])
        if identity in seen:
            raise StructureError('CHAPTER_FACTS_DUPLICATE', 'the same fact was selected twice')
        seen.add(identity)
        attribute = attributes.get(slot)
        if any(row.get('status') != 'archived' and row['contentRole'] == 'fact'
               and row['ownerId'] == fact['ownerId'] and row['text'] == fact['statement']
               and attribute in row['contentBindings'] for row in source['fragments']):
            raise StructureError('CHAPTER_FACTS_ALREADY_EXISTS', 'this exact owner/topic fact already exists; cite its existing identity',
                                 {'ownerId': fact['ownerId'], 'fieldId': field['fieldId']})
        if attribute is None:
            attribute = f'$chapter-fact-attribute-{number}'
            attributes[slot] = attribute
            changes.append({'action': 'put', 'collection': 'attributes', 'record': {
                'attributeId': attribute, 'ownerId': fact['ownerId'], 'fieldId': field['fieldId'], 'values': []}})
        fragment = f'$chapter-fact-fragment-{number}'
        orders[fact['ownerId']] += 1
        changes.append({'action': 'put', 'collection': 'fragments', 'record': {
            'fragmentId': fragment, 'ownerId': fact['ownerId'], 'text': fact['statement'],
            'order': orders[fact['ownerId']], 'contentBindings': [attribute],
            'dependsOn': list(dependencies), 'contentRole': 'fact'}})
        selected.append({'itemId': f'fact-{number}', 'ownerId': fact['ownerId'], 'fieldId': field['fieldId'],
                         'attributeId': attribute, 'fragmentId': fragment, 'quote': quoted, 'reason': fact['reason'],
                         **({'findingId': fact['findingId']} if 'findingId' in fact else {})})
    binding = {'schemaVersion': 1, 'chapterId': chapter_id, 'input': {'chapterId': chapter_id, 'facts': deepcopy(facts)},
               'manuscript': manuscript, 'continuity': continuity, 'guards': guards, 'facts': selected}
    return changes, binding


def validate_fact_plan_inputs(root, plan, *, current):
    """Replay the typed assembler at source preview and ordinary source commit."""
    binding = plan['parameters']['chapterFacts']
    if not isinstance(binding, dict) or not isinstance(binding.get('input'), dict):
        raise StructureError('CHAPTER_FACTS_BINDING_INVALID', 'chapter facts need their original explicit input')
    changes, expected = _assemble(root, current, **{'chapter_id': binding['input'].get('chapterId'),
                                                  'facts': binding['input'].get('facts')})
    if binding != expected or plan['operation'] != 'source_change':
        raise StructureError('CHAPTER_FACTS_INPUT_CHANGED', 'chapter fact inputs differ from the prepared snapshot')
    identities = plan['parameters']['identityMap']
    proposed = deepcopy(current)
    for change in changes:
        record = deepcopy(change['record'])
        key = 'attributeId' if change['collection'] == 'attributes' else 'fragmentId'
        record[key] = identities.get(record[key], record[key])
        if 'contentBindings' in record:
            record['contentBindings'] = [identities.get(value, value) for value in record['contentBindings']]
        proposed[change['collection']].append(record)
    ignored = {'revision', 'updatedAt', 'lastOperation'}
    if {k: v for k, v in proposed.items() if k not in ignored} != {
            k: v for k, v in plan['proposedStructure'].items() if k not in ignored}:
        raise StructureError('CHAPTER_FACTS_BINDING_INVALID', 'source preview differs from the explicit chapter facts')
    return deepcopy(binding['guards'])


def _binding(prior):
    binding = prior.get('chapterFacts', prior.get('parameters', {}).get('chapterFacts'))
    if not isinstance(binding, dict) or binding.get('schemaVersion') != 1:
        raise StructureError('SOURCE_REQUEST_REUSED', 'requestId identifies a different source operation')
    return binding


def _rows(prior, binding):
    identities = prior.get('identityMap', prior.get('parameters', {}).get('identityMap', {}))
    return [{**deepcopy(row), 'attributeId': identities.get(row['attributeId'], row['attributeId']),
             'fragmentId': identities.get(row['fragmentId'], row['fragmentId'])} for row in binding['facts']]


def _citations(rows):
    return [{**row['quote'], 'targetIds': [row['fragmentId']], 'kind': 'depends_on', 'reason': row['reason']} for row in rows]


def _evidence_progress(root, prior, binding, rows):
    from .evidence_submission import _resolve, _completed, _ledger
    from .source_evidence import evidence_rows, read_evidence
    evidence_request = 'chapter-facts:' + prior['operationId']
    states = {row['fragmentId']: {'evidenceStatus': 'not_registered', 'evidenceId': None} for row in rows}
    current = {row['evidenceId']: row for row in evidence_rows(root, load_structure(root))[0]}
    # After a source change, the caller may explicitly reread and register exact
    # citations with the ordinary evidence tools. Their current domain receipts
    # satisfy this request without silently rebinding its original submission.
    expected_rows = _citations(rows)
    for receipt, batch in read_evidence(root)['batches'].items():
        if (batch['chapterId'] != binding['chapterId'] or batch['fileHash'] != binding['manuscript']['sha256']
                or batch['path'] != binding['manuscript']['path']):
            continue
        for citation in batch['citations']:
            if (current[citation['evidenceId']]['status'] == 'registered'
                    and {key: citation[key] for key in ('start', 'end', 'text', 'targetIds', 'kind', 'reason')} in expected_rows):
                states[citation['targetIds'][0]] = {'evidenceStatus': 'registered',
                    'evidenceId': citation['evidenceId'], 'receiptId': receipt}
    try:
        value = _resolve(root, request_id=evidence_request)
    except StructureError as exc:
        if exc.code != 'EVIDENCE_REQUEST_NOT_FOUND':
            raise
        return evidence_request, states, False
    expected = {'chapterId': binding['chapterId'], 'fileHash': binding['manuscript']['sha256'],
                'sourceHash': binding.get('committedSourceHash'), 'requestId': evidence_request,
                'citations': _citations(rows)}
    if value['operation'] != 'source.evidence-register' or value['arguments'] != expected:
        raise StructureError('SOURCE_REQUEST_REUSED', 'evidence request is bound to different chapter facts')
    _, ledger = _ledger(root, value['operation'])
    for receipt in _completed(root, value).values():
        for citation in ledger['batches'][receipt]['citations']:
            state = current[citation['evidenceId']]
            if states[citation['targetIds'][0]]['evidenceStatus'] != 'registered':
                states[citation['targetIds'][0]] = {'evidenceStatus': state['status'],
                                                   'evidenceId': citation['evidenceId'], 'receiptId': receipt}
    return evidence_request, states, True


def _report(root, source, state, prior):
    from .source_reports import public_report
    from .source_requests import stale_reasons
    binding = _binding(prior)
    rows = _rows(prior, binding)
    committed = state == 'committed'
    evidence_request, evidence, started = _evidence_progress(root, prior, binding, rows) if committed else (
        'chapter-facts:' + prior['operationId'], {}, False)
    facts = [{**{key: row[key] for key in ('itemId', 'ownerId', 'fieldId', 'attributeId', 'fragmentId')},
              'quote': deepcopy(row['quote']),
              **({'findingId': row['findingId']} if 'findingId' in row else {}),
              **evidence.get(row['fragmentId'], {'evidenceStatus': 'not_registered', 'evidenceId': None})} for row in rows]
    complete = committed and all(row['evidenceStatus'] == 'registered' for row in facts)
    current = committed and snapshot_hash(source) == binding.get('committedSourceHash')
    blockers, next_action = [], None
    if state == 'prepared':
        next_action = public_report(prior, root=root).get('nextAction')
        blockers.append({'code': 'CHAPTER_FACTS_COMMIT_REQUIRED', 'message': 'source preview still needs explicit author approval and commit'})
    elif state == 'stale':
        reasons = stale_reasons(root, source, prior)
        blockers.append({'code': 'CHAPTER_FACTS_PREVIEW_STALE', 'staleReasons': reasons})
        if reasons == ['expired']:
            next_action = _action('source.request-renew', requestId=prior['requestId'])
    elif state == 'undone':
        blockers.append({'code': 'CHAPTER_FACTS_UNDONE', 'message': 'the source transaction was undone'})
    elif committed:
        index = SourceIndex.build(source)
        for row, explicit in zip(rows, binding['input']['facts']):
            fragment = index.fragments.get(row['fragmentId'], {})
            attribute = index.attributes.get(row['attributeId'], {})
            field = index.fields.get(row['fieldId'], {})
            if (fragment.get('ownerId') != row['ownerId'] or fragment.get('text') != explicit['statement']
                    or fragment.get('contentRole') != 'fact' or fragment.get('status') == 'archived'
                    or fragment.get('contentBindings') != [row['attributeId']]
                    or fragment.get('dependsOn', []) != explicit.get('dependsOn', [])
                    or attribute.get('ownerId') != row['ownerId'] or attribute.get('fieldId') != row['fieldId']
                    or field.get('valueType') != 'topic' or index.objects.get(row['ownerId'], {}).get('status') == 'archived'):
                complete = False
                blockers.append({'code': 'CHAPTER_FACTS_TARGET_CHANGED', 'fragmentId': row['fragmentId'],
                                 'message': 'a committed fact no longer matches this explicit request'})
        try:
            manuscript, continuity, _, _ = _formal_chapter(root, source, binding['chapterId'])
            if manuscript != binding['manuscript'] or continuity != binding['continuity']:
                raise StructureError('CHAPTER_FACTS_PROSE_CHANGED', 'chapter provenance changed after the fact preview')
        except (ValueError, OSError) as exc:
            complete = False
            blockers.append({'code': getattr(exc, 'code', 'CHAPTER_FACTS_INPUT_CHANGED'), 'message': str(exc),
                             'details': getattr(exc, 'details', {})})
        if not current and not complete:
            blockers.append({'code': 'CHAPTER_FACTS_SOURCE_CHANGED',
                             'message': 'source changed after fact commit; reread before registering remaining evidence'})
            next_action = _action('evidence.prepare', kind='prose', chapterId=binding['chapterId'])
        elif not complete and not blockers:
            next_action = _action('source.chapter-facts-complete', requestId=prior['requestId'], registerEvidence=True)
    return {'ok': not blockers, 'code': 'CHAPTER_FACTS_COMPLETE' if complete else 'CHAPTER_FACTS_PENDING',
            'requestId': prior['requestId'], 'chapterId': binding['chapterId'], 'operationId': prior['operationId'],
            'operationState': state, 'committed': committed, 'complete': complete,
            'manuscript': deepcopy(binding['manuscript']),
            'sourceSnapshotCurrent': current, 'evidenceRequestId': evidence_request, 'evidenceStarted': started,
            'evidenceComplete': complete, 'factCount': len(facts), 'facts': facts,
            'pendingEvidenceCount': sum(row['evidenceStatus'] != 'registered' for row in facts),
            'readyToRegisterEvidence': committed and current and not blockers and not complete,
            'blockers': blockers, 'nextAction': next_action,
            'statusAction': _action('source.chapter-facts-complete', requestId=prior['requestId']),
            'semanticReviewComplete': False, 'approvalGranted': False,
            'coverage': {'scope': 'explicit facts in this request only', 'allChapterFactsIdentified': False}}


def preview_chapter_facts(root: Path, *, requestId: str, chapterId: str | None = None, facts: list | None = None):
    """Prepare new topic facts; requestId alone resumes the existing source request."""
    from .source_changes import _preview_locked
    from .source_requests import resume_request
    from .query import _size
    root = root.expanduser().resolve()
    if _size({'requestId': requestId, 'chapterId': chapterId}) * 4 > query_policy()['maxBytes']['detail']:
        raise StructureError('CHAPTER_FACTS_BUDGET_INVALID', 'request identity exceeds the response budget')
    with project_lock_reentrant(root):
        require_no_pending(root)
        source = load_structure(root)
        require_current_source(source)
        operation_id, _ = request_identity(source, requestId)
        state, prior = request_state(root, source, operation_id)
        if prior is not None:
            binding = _binding(prior)
            if ((chapterId is not None and chapterId != binding['input']['chapterId'])
                    or (facts is not None and facts != binding['input']['facts'])):
                raise StructureError('SOURCE_REQUEST_REUSED', 'requestId identifies different explicit facts')
            preview = resume_request(root, source, operation_id, prior['requestFingerprint'])
        else:
            changes, binding = _assemble(root, source, chapterId, facts)
            preview = _preview_locked(root, changes=changes, request_id=requestId, base_revision=source['revision'],
                reason='\n'.join(row['reason'] for row in facts), actor='app', chapter_facts=binding)
        state, prior = request_state(root, source, operation_id)
        report = _report(root, source, state, prior)
        return _bounded_report(root, {**report, 'ok': state == 'prepared' or report['ok'],
                'code': 'CHAPTER_FACTS_PREPARED' if state == 'prepared' else report['code'], 'sourcePreview': preview})


def complete_chapter_facts(root: Path, *, requestId: str, registerEvidence: bool = False):
    """Read request progress, optionally resume explicit evidence registration."""
    from .evidence_pack import _prepare_snapshot, pack_evidence
    from .evidence_submission import submit_evidence
    root = root.expanduser().resolve()
    if type(registerEvidence) is not bool:
        raise StructureError('CHAPTER_FACTS_INPUT_INVALID', 'registerEvidence must be a boolean')
    with project_lock_reentrant(root):
        require_no_pending(root)
        source = load_structure(root)
        operation_id, _ = request_identity(source, requestId)
        state, prior = request_state(root, source, operation_id)
        if prior is None:
            raise StructureError('CHAPTER_FACTS_REQUEST_NOT_FOUND', 'preview explicit chapter facts before querying completion')
        report = _report(root, source, state, prior)
        if not registerEvidence or not report['readyToRegisterEvidence']:
            if registerEvidence and report['complete']:
                report['workflowHandoff'] = _refresh_completed_handoff(root)
            return _bounded_report(root, report)
        binding = _binding(prior)
        request = report['evidenceRequestId']
        if report['evidenceStarted']:
            result = submit_evidence(root, requestId=request)
        else:
            snapshot, _ = _prepare_snapshot(root, kind='prose', chapterId=binding['chapterId'])
            if snapshot['sourceHash'] != binding['committedSourceHash'] or snapshot['fileHash'] != binding['manuscript']['sha256']:
                raise StructureError('CHAPTER_FACTS_INPUT_CHANGED', 'source or manuscript changed before evidence registration')
            packed = pack_evidence(root, snapshot=snapshot, requestId=request, view='model', decisions=[{
                'quote': {'text': row['quote']['text'], 'start': row['quote']['start'], 'end': row['quote']['end']},
                'targetIds': [row['fragmentId']], 'kind': 'depends_on', 'reason': row['reason']}
                for row in _rows(prior, binding)])
            result = submit_evidence(root, submissionRef=packed['submissionRef'])
        refreshed = _report(root, load_structure(root), state, prior)
        if result.get('ok') is False:
            refreshed['ok'] = False
        if refreshed['complete']:
            refreshed['workflowHandoff'] = _refresh_completed_handoff(root)
        return _bounded_report(root, {**refreshed, 'evidenceSubmission': result})


def _refresh_completed_handoff(root):
    """Resume the derived workflow after the independently committed evidence.

    Failure here never hides a completed evidence receipt. The same request can
    retry this mechanical handoff; no approval or narrative decision is created.
    """
    from .finalization import _reconcile_workflow_locked
    try:
        return _reconcile_workflow_locked(root, persist=True)
    except (ValueError, OSError, RuntimeError) as exc:
        return {'ok': False, 'code': getattr(exc, 'code', 'FACT_HANDOFF_REFRESH_FAILED'),
                'message': str(exc), 'nextAction': _action('project.workflow.reconcile', persist=True)}
