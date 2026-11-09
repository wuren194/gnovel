"""Compact model views preserve unresolved work while removing repeated metadata."""
import json
import tempfile
import unittest
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from scripts.core.context_transport import task_model_response
from scripts.core.diagnostics_diff import diff_findings, diff_reports, model_diff
from scripts.core.jsonio import write_json
from scripts.core.task_prepare import prepare_task
from tests.fixtures_workflow import review_ready_project
from tests.test_diagnostics_diff import cliche, cli
from unittest.mock import patch


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


_open_events = None


def _audit_open(event, args):
    if event == 'open' and _open_events is not None:
        _open_events.append(str(args[0]))


sys.addaudithook(_audit_open)


@contextmanager
def count_opens():
    global _open_events
    events = []
    _open_events = events
    try:
        yield events
    finally:
        _open_events = None


class ModelEfficiencyTests(unittest.TestCase):
    def test_complete_editor_transport_and_repair_budget(self):
        from scripts.core.operations import call_operation
        from scripts.core.static_cache import _FILES, _VALUES
        from scripts.core.jsonio import read_json
        from scripts.core.project import TOOLBOX_ROOT
        budgets = read_json(TOOLBOX_ROOT / 'tests/fixtures/model_efficiency_budgets.json')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            chapter = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            _FILES.clear(); _VALUES.clear()
            args = {'task': 'editor', 'chapterId': chapter, 'view': 'model'}
            with count_opens() as opened:
                report = call_operation('task.prepare', root, args)
            self.assertLessEqual(len(opened), budgets['prepareColdFileOpens'])
            self.assertLessEqual(size(report), budgets['prepareCharacters'])
            request_chars, response_chars, calls = size(args), size(report), 1
            reads = {'credential': report['reading']['credential'],
                     'readIds': [rid for row in report['contractReads'] for rid in row['readIds']]}
            cursor = None; pages = 0; pieces = {}
            with count_opens() as opened, patch('scripts.core.reading_batch.prepare_task', side_effect=AssertionError('reprepared')):
                while True:
                    args = {**reads, **({'cursor': cursor} if cursor else {})}
                    page = call_operation('task.read-batch', root, args)
                    pages += 1; calls += 1; request_chars += size(args); response_chars += size(page)
                    for row in page['items']:
                        pieces.setdefault(row['id'], []).append(row)
                    if not page['hasMore']: break
                    cursor = page['nextCursor']
                self.assertLessEqual(len(opened), budgets['firstReadFileOpens'])
                self.assertLessEqual(pages, budgets['firstReadPages'])
                args = {**reads, 'priorReceipt': page['sessionReadReceipt']}
                repeated = call_operation('task.read-batch', root, args)
                calls += 1; request_chars += size(args); response_chars += size(repeated)
            self.assertFalse(repeated['hasMore']); self.assertEqual(repeated['items'], [])
            self.assertEqual(repeated['unchangedSummary']['chunks'], len(pieces))
            self.assertLessEqual(size(repeated), budgets['unchangedResponseCharacters'])
            for chunks in pieces.values():
                self.assertEqual(chunks[0]['start'], 0)
                for a, b in zip(chunks, chunks[1:]): self.assertEqual(a['end'], b['start'])
                first = chunks[0]
                raw = (TOOLBOX_ROOT / first['path']).read_bytes().decode()
                expected = ''.join(raw.splitlines(keepends=True)[first['startLine']-1:first['endLine']])
                self.assertEqual(''.join(row['text'] for row in chunks), expected)
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            args = {'credential': report['reading']['credential'], 'requestId': 'budget-repair',
                    'patches': [{'issueId': 'one', 'original': {'text': '合上了登记本'}, 'replacement': '翻开了登记本'}],
                    'advanceLifecycle': True, 'diagnostics': {'mode': 'plot', 'intensityLevel': 1}}
            result = call_operation('workflow.patch-candidate', root, args)
            calls += 1; request_chars += size(args); response_chars += size(result)
            self.assertTrue(result['ok'], result)
            self.assertIn('repairContext', result); self.assertFalse(result['approvalPerformed'])
            self.assertFalse(result['semanticReviewComplete'])
            self.assertNotIn('evidence', result['diagnostics'])
            self.assertLessEqual(calls, budgets['totalCalls'])
            self.assertLessEqual(request_chars, budgets['requestCharacters'])
            self.assertLessEqual(response_chars, budgets['totalResponseCharacters'])
    def test_unchanged_findings_keep_every_anchor_current_line_and_severity(self):
        before = [cliche(f'S-{i}', f'词{i}', i) for i in range(50)]
        after = [cliche(f'S-{i}', f'词{i}', i + 4) for i in range(50)]
        full = diff_findings(before, after)
        original = deepcopy(full)
        model = model_diff(full)
        self.assertEqual(full, original)
        self.assertEqual(model['summary'], full['summary'])
        self.assertEqual(model['groups'], [])
        expected = {(tuple(row['anchor']['identity']), tuple(row['after']['lines']),
                     row['after']['count'], tuple(row['after']['severities'])) for row in full['groups']}
        actual = {(tuple(row['identity']), tuple(row['lines']), row['count'], tuple(row['severities']))
                  for source in model['persisting'] for row in source['items']}
        self.assertEqual(actual, expected)
        self.assertLess(size(model), size(before))
        self.assertFalse(model['coverage']['semanticFixVerified'])

    def test_changed_counts_severity_and_unmatched_are_not_collapsed(self):
        before = [cliche('S-1', '词', 1), cliche('S-2', '词', 2), {'source': 'validate_text'}]
        after = [cliche('S-1', '词', 1), cliche('S-1', '词', 2), cliche('S-2', '词', 3, 'error'),
                 {'source': 'validate_text'}]
        full = diff_findings(before, after)
        model = model_diff(full)
        self.assertEqual(model['groups'], full['groups'])
        self.assertEqual(model['unmatchedBefore'], full['unmatchedBefore'])
        self.assertEqual(model['unmatchedAfter'], full['unmatchedAfter'])
        self.assertEqual(model['persisting'], [])

    def test_prepare_keeps_reading_and_blocker_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            chapter = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            full = prepare_task(root, 'editor', chapter_id=chapter)
            original = deepcopy(full)
            model = task_model_response(full)
            self.assertEqual(full, original)
            for key in ('missing', 'readiness', 'scope', 'nextAction', 'actionReadiness'):
                self.assertEqual(model[key], full[key])
            self.assertEqual(model['modelContext'], full['context']['modelContext'])
            self.assertEqual(len(model['reading']['referenceReads']), len(full['reading']['referenceReads']))
            for original_reference, compact_reference in zip(full['reading']['referenceReads'], model['reading']['referenceReads']):
                expected_reference = deepcopy(original_reference)
                if expected_reference.get('referencePath') == expected_reference.get('path'):
                    expected_reference.pop('referencePath', None)
                self.assertEqual(compact_reference, expected_reference)
            referenced = {row.get('referencePath', row.get('path'))
                          for row in model['reading']['referenceReads']}
            self.assertEqual(set(model['onDemandReferences']),
                             set(full['promptPlan']['onDemandReferences']) - referenced)
            self.assertNotIn('scopeHash', model['reading'])
            self.assertEqual(model['reading']['credential'], full['reading']['credential'])
            for old, new in zip(full['toolPlan']['toolActions'], model['toolPlan']['toolActions']):
                for key in ('capabilityId', 'binding', 'status', 'necessary', 'reason', 'authorization', 'observed', 'inputsReady'):
                    self.assertEqual(old[key], new[key])
            self.assertLess(size(model['toolPlan']), size(full['toolPlan']))
            self.assertEqual(model['toolPlan']['unresolved'], full['toolPlan']['unresolved'])
            self.assertEqual(model['authorization']['pendingApprovals'], full['toolPlan']['pendingApprovals'])
            self.assertNotIn('pendingApprovals', model['toolPlan'])

    def test_compact_tool_names_recover_complete_schemas_from_existing_interfaces(self):
        from scripts.core.operations import call_operation
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            chapter = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            full = prepare_task(root, 'editor', chapter_id=chapter)
            original = deepcopy(full)
            model = task_model_response(full)
            self.assertEqual(model['readingTools'], {
                row['operation']: row['tool'] for row in full['readingTools']})
            self.assertIn('capabilities.list(operation=', model['readingToolsGuide'])
            self.assertIn('task.snapshot(reference=prepareRef)', model['readingToolsGuide'])
            pieces, cursor = [], None
            while True:
                page = call_operation('task.snapshot', root, {'reference': model['prepareRef'],
                                      **({'cursor': cursor} if cursor else {})})
                pieces.extend(row['text'] for row in page['materials'])
                if not page['hasMore']:
                    break
                cursor = page['nextCursor']
            restored = json.loads(''.join(pieces))
            self.assertEqual(restored['readingTools'], full['readingTools'])
            for row in restored['readingTools']:
                definition = call_operation('capabilities.list', root, {'operation': row['operation']})['operations'][0]
                self.assertEqual(definition['name'], row['tool'])
                self.assertEqual(definition['inputSchema'], row['inputSchema'])
            submit = next(row for row in restored['readingTools'] if row['operation'] == 'workflow.submit')
            self.assertIn('anyOf', submit['inputSchema'])
            self.assertEqual(submit['inputSchema']['required'], ['requestId'])
            for key in ('missing', 'notes', 'readiness', 'scope', 'nextAction', 'actionReadiness'):
                self.assertEqual(model[key], full[key])
            self.assertEqual(model['modelContext'], full['context']['modelContext'])
            self.assertEqual(model['reviewPlan'], full['promptPlan']['reviewPlan'])
            self.assertEqual(full, original)

    def test_model_omits_only_absent_constraint_boilerplate_and_machine_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            chapter = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            full = prepare_task(root, 'editor', chapter_id=chapter)
            original = deepcopy(full)
            model = task_model_response(full)
            self.assertEqual(model['outlineFacts']['projectConstraints'], {'source': 'AGENTS.md', 'available': False})
            self.assertNotIn('sourcePlanHash', model['outlineFacts'])
            self.assertEqual(full, original)
            constraints = {'source': 'AGENTS.md', 'sha256': 'a' * 64,
                'ranges': [{'range': [2, 3], 'quote': '本卷限制', 'line': 2}],
                'conflicts': [{'declaredLevel': 1, 'quote': '本卷限制', 'line': 2}],
                'exceptionDeclared': True, 'exceptionApproved': False,
                'exceptionDeclaration': '尚需作者裁定', 'requiresAuthorExceptionApproval': True,
                'coverage': '只识别明确范围，自由文本必须另读'}
            full['outlineFacts']['projectConstraints'] = constraints
            full['missing'] = [{'code': 'PROJECT_CONFLICT', 'message': '必须裁定', 'blocking': True}]
            model = task_model_response(full)
            self.assertEqual(model['outlineFacts']['projectConstraints'], constraints)
            self.assertEqual(model['missing'], full['missing'])
            self.assertEqual(model['readList'], task_model_response(original)['readList'])

    def test_cli_model_diff_is_default_and_explicit_full_preserves_all_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chapter = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            before, after = root / 'before.json', root / 'after.json'
            report = {'findings': [cliche('S-1', '词', 1)], 'draftSha256': 'a' * 64}
            write_json(before, report); write_json(after, report)
            full = cli('project', 'workflow', 'diagnostics-diff', '--project-root', str(root),
                       '--chapter-id', chapter, '--current', str(after), '--against', str(before), '--view', 'full')
            compact = cli('project', 'workflow', 'diagnostics-diff', '--project-root', str(root),
                          '--chapter-id', chapter, '--current', str(after), '--against', str(before), '--view', 'model')
            default = cli('project', 'workflow', 'diagnostics-diff', '--project-root', str(root),
                          '--chapter-id', chapter, '--current', str(after), '--against', str(before))
            self.assertTrue(compact['ok'])
            self.assertEqual(default, compact)
            self.assertEqual(full['groups'], diff_reports(before, after)['groups'])
            self.assertEqual(compact['summary'], full['summary'])
            self.assertEqual(compact['persisting'][0]['items'][0]['lines'], [1])
