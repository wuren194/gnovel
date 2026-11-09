"""Version-bound repair anchors exercise the real patch, receipt and guard paths."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.candidate_patch import patch_candidate
from scripts.core.edit_anchors import read_anchor
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS
from scripts.core.structure_store import StructureError
from scripts.core.transport_cache import _directory, get, put
from scripts.core.workflow import registered_artifact, require_approved_artifact, WorkflowError
from tests.fixtures_workflow import review_ready_project


class CandidateAnchorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        fixture = review_ready_project(self.root, chapter_count=2)
        self.chapter, self.other_chapter = fixture['chapterIds']
        text = ('前文保持不变。' * 40 + '\r\n甲推开门。\r\n同一句。\r\n原样😀\r\n'
                '同一句。\r\n乙合上书。\r\n' + '后文保持不变。' * 40)
        self.install(text, [('door', '甲推开门。'), ('repeat', '同一句。'), ('book', '乙合上书。')])

    def install(self, text, locations):
        self.text = text
        (self.root / '工作台/anchor-base.txt').write_bytes(text.encode())
        self.base = pack_artifact(self.root, chapterId=self.chapter, artifactType='candidate_prose',
                                  input='工作台/anchor-base.txt')
        payload = {'verdict': 'REPAIR_REQUIRED', 'repairScope': 'local',
                   'slots': {slot: {'conclusion': '需核对'} for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES)},
                   'findings': [{'severity': 'S2', 'ruleId': rule, 'summary': f'核对 {rule} 的动作衔接',
                                 'location': quote, 'repairSuggestion': '由工位判断修订文字'}
                                for rule, quote in locations]}
        write_json(self.root / '工作台/anchor-review.json', payload)
        pack_artifact(self.root, chapterId=self.chapter, artifactType='stage_b_review',
                      input='工作台/anchor-review.json')
        self.items = self.context_items()

    def context_items(self, **arguments):
        items, cursor = [], None
        while True:
            page = call_operation('workflow.repair-context', self.root, {'chapterId': self.chapter,
                **arguments, **({'cursor': cursor} if cursor else {})})
            self.assertFalse(page['coverage']['semanticFixVerified'])
            items.extend(page['items'])
            if not page['hasMore']:
                return items
            cursor = page['nextCursor']

    def issue(self, rule, occurrence=0):
        return next(row for row in self.items if row['source'] == 'issuePreview'
                    and row['finding']['ruleId'] == rule
                    and row['location']['occurrenceIndex'] == occurrence)

    def repair(self, rule='door', replacement='甲推开窗。', occurrence=0):
        issue = self.issue(rule, occurrence)
        return {'issueId': issue['issueId'], 'anchorRef': issue['location']['spans'][0]['anchorRef'],
                'replacement': replacement}

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def body(self, result):
        return read_json(self.root / result['artifactPath'])['payload']['text']

    def assert_rejected(self, arguments, code=None):
        before = self.files()
        with self.assertRaises(ValueError) as caught:
            patch_candidate(self.root, **arguments)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.files(), before)

    def assert_eof_location_stays_readable(self, newline):
        self.install('末行。' + newline, [('body', '末行。')])
        finding = {'severity': 'S3', 'ruleId': 'EOF-advisory', 'summary': '核对末尾空行',
                   'location': '', 'line': 2}
        path = self.root / '工作台/anchor-review.json'
        payload = read_json(path)
        payload['findings'].append(finding)
        write_json(path, payload)
        pack_artifact(self.root, chapterId=self.chapter, artifactType='stage_b_review',
                      input='工作台/anchor-review.json')
        before = self.files()
        self.items = self.context_items()
        self.assertEqual(self.files(), before)
        eof = self.issue('EOF-advisory')
        self.assertEqual(eof['location']['state'], 'line')
        self.assertEqual(eof['location']['spans'], [{'start': len(self.text), 'end': len(self.text),
            'startLine': 2, 'endLine': 2, 'text': '', 'textTruncated': False}])
        self.assertEqual({row['finding']['ruleId'] for row in self.items if row['source'] == 'issuePreview'
                          and row.get('reviewState') == 'reported'},
                         {'body', 'EOF-advisory'})
        window = next(row for row in self.items if row['source'] == 'contextWindow' and row['id'] in eof['windowIds'])
        self.assertEqual(window['text'], self.text)
        anchor = read_anchor(self.root, window['anchorRef'])
        self.assertLess(anchor['start'], anchor['end'])
        raw = json.loads(''.join(row['text'] for row in self.items
                         if row['source'] == 'stage_b_review' and row['finding']['ruleId'] == 'EOF-advisory'))
        self.assertEqual(raw['finding'], finding)
        self.assertNotIn('anchorRef', raw['location']['spans'][0])
        result = patch_candidate(self.root, patches=[self.repair('body', '末句。')])
        self.assertEqual(self.body(result), '末句。' + newline)
        self.assertFalse(result['approvalPerformed'])
        self.assertFalse(result['semanticReviewComplete'])

    def test_lf_eof_location_does_not_block_other_repair_anchors(self):
        self.assert_eof_location_stays_readable('\n')

    def test_crlf_eof_location_does_not_block_other_repair_anchors(self):
        self.assert_eof_location_stays_readable('\r\n')

    def test_pure_anchor_batch_uses_one_baseline_without_transferring_approvals(self):
        before = read_json(self.root / '.govel/workflow.json')
        patches = [self.repair('book', '乙放下笔。'), self.repair()]
        with patch('scripts.core.mechanical_evidence.run_diagnostics', side_effect=AssertionError('not requested')):
            result = call_operation('workflow.patch-candidate', self.root, {'patches': patches})
        self.assertEqual(self.body(result), self.text.replace('甲推开门。', '甲推开窗。').replace('乙合上书。', '乙放下笔。'))
        self.assertEqual(self.body(self.base), self.text)
        self.assertEqual(result['baseCandidateSha256'], self.base['artifactSha256'])
        self.assertEqual([row['start'] for row in result['patches']], sorted(row['start'] for row in result['patches']))
        self.assertFalse(result['approvalPerformed']); self.assertFalse(result['semanticReviewComplete'])
        self.assertTrue(result['mechanicalChecksRequired'])
        after = read_json(self.root / '.govel/workflow.json')
        self.assertEqual(after['approvals'], before['approvals'])
        for kind in ('diagnostics', 'stage_b_review', 'state_candidate'):
            self.assertIsNone(registered_artifact(after, self.chapter, kind))
        with self.assertRaises(WorkflowError):
            require_approved_artifact(after, self.chapter, 'candidate_prose')
        self.assert_rejected({'patches': patches}, 'PATCH_BASE_STALE')

    def test_window_subrange_uses_python_characters_and_preserves_crlf(self):
        issue = self.issue('repeat', 1)
        window = next(row for row in self.items if row['source'] == 'contextWindow' and row['id'] in issue['windowIds'])
        self.assertGreater(window['fileStart'], 0)
        quote = '原样😀\r\n同一句。'
        absolute = self.text.index(quote)
        relative = absolute - window['fileStart']
        result = patch_candidate(self.root, patches=[{'issueId': issue['issueId'], 'anchorRef': window['anchorRef'],
            'range': {'start': relative, 'end': relative + len(quote)}, 'replacement': '原样😀\r\n另句话。'}])
        self.assertEqual(self.body(result), self.text.replace(quote, '原样😀\r\n另句话。'))
        self.assertEqual(result['patches'][0]['start'], absolute)
        self.assertEqual(result['patches'][0]['end'], absolute + len(quote))

    def test_whitespace_only_crlf_subrange_can_be_repaired(self):
        issue = self.issue('door')
        window = next(row for row in self.items if row['source'] == 'contextWindow' and row['id'] in issue['windowIds'])
        absolute = self.text.index('\r\n', self.text.index('甲推开门。'))
        start = absolute - window['fileStart']
        result = call_operation('workflow.patch-candidate', self.root, {'patches': [{
            'issueId': issue['issueId'], 'anchorRef': window['anchorRef'],
            'range': {'start': start, 'end': start + 2}, 'replacement': '\n'}]})
        self.assertEqual(self.body(result), self.text[:absolute] + '\n' + self.text[absolute + 2:])
        self.assertEqual(result['patches'][0]['end'] - result['patches'][0]['start'], 2)

    def test_window_range_does_not_restart_at_transport_page(self):
        text = '甲' * 3000 + '独特😀\r\n需改处' + '乙' * 3000
        self.install(text, [('long', text)])
        chunks = [row for row in self.context_items(maxBytes=3000) if row['source'] == 'contextWindow']
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(row['text'] for row in chunks), text)
        selected = next(row for row in chunks if '需改处' in row['text'])
        self.assertGreater(selected['start'], 0)
        start = selected['start'] + selected['text'].index('需改处')
        result = patch_candidate(self.root, patches=[{'issueId': self.issue('long')['issueId'],
            'anchorRef': selected['anchorRef'], 'range': {'start': start, 'end': start + len('需改处')},
            'replacement': '已改处'}])
        self.assertEqual(self.body(result), text.replace('需改处', '已改处'))

    def test_repeated_quote_anchor_selects_only_the_explicit_occurrence(self):
        first, second = self.issue('repeat'), self.issue('repeat', 1)
        self.assertEqual(first['issueId'], second['issueId'])
        self.assertEqual(second['location']['state'], 'ambiguous')
        self.assertNotEqual(first['location']['spans'][0]['anchorRef'], second['location']['spans'][0]['anchorRef'])
        result = patch_candidate(self.root, patches=[self.repair('repeat', '只改第二处。', 1)])
        start = second['location']['spans'][0]['start']
        self.assertEqual(self.body(result), self.text[:start] + '只改第二处。' + self.text[start + len('同一句。'):])
        self.assertIn('同一句。', self.body(result))

    def test_anchor_with_explicit_version_can_mix_legacy_quotes(self):
        result = patch_candidate(self.root, chapterId=self.chapter, candidateSha256=self.base['artifactSha256'],
            patches=[self.repair(), {'issueId': 'legacy-book', 'original': {'text': '乙合上书。'}, 'replacement': '乙放下笔。'}])
        self.assertEqual(self.body(result), self.text.replace('甲推开门。', '甲推开窗。').replace('乙合上书。', '乙放下笔。'))

    def test_preparation_credential_and_anchor_must_agree(self):
        report = call_operation('task.prepare', self.root, {'task': 'repair', 'chapterId': self.chapter})
        result = patch_candidate(self.root, credential=report['reading']['credential'], patches=[self.repair()])
        self.assertEqual(self.body(result), self.text.replace('甲推开门。', '甲推开窗。'))
        self.assert_rejected({'credential': report['reading']['credential'], 'patches': [self.repair()]}, 'PATCH_BASE_STALE')

    def test_issue_scope_subrange_and_patch_shape_errors_leave_no_writes(self):
        row = self.repair(); length = len('甲推开门。')
        cases = [({**row, 'issueId': 'unrelated-issue'}, 'EDIT_ANCHOR_ISSUE_MISMATCH')]
        cases += [({**row, 'range': value}, None) for value in (
            {'start': -1, 'end': 1}, {'start': 0, 'end': length + 1}, {'start': 2, 'end': 2},
            {'start': 3, 'end': 1}, {'start': False, 'end': 1}, {'start': 0}, None)]
        cases += [({**row, 'original': {'text': '甲推开门。'}}, None), ({**row, 'issueId': ' '}, None),
                  ({**row, 'replacement': None}, None)]
        for candidate, code in cases:
            with self.subTest(candidate=candidate):
                self.assert_rejected({'patches': [candidate]}, code)
        self.assert_rejected({'patches': []}, 'PATCH_INVALID')

    def test_overlapping_anchor_and_quote_or_duplicate_anchor_are_rejected_atomically(self):
        row = self.repair()
        for patches in ([row, row], [row, {'issueId': 'legacy', 'original': {'text': '推开门'}, 'replacement': '推窗'}]):
            self.assert_rejected({'patches': patches}, 'PATCH_OVERLAP')

    def test_cross_project_chapter_and_explicit_version_are_rejected(self):
        row = self.repair()
        self.assert_rejected({'patches': [row], 'chapterId': self.other_chapter}, 'EDIT_ANCHOR_SCOPE_MISMATCH')
        self.assert_rejected({'patches': [row], 'candidateSha256': 'not-the-version'}, 'PATCH_BASE_STALE')
        value = read_anchor(self.root, row['anchorRef'])
        for key, other in (('chapterId', self.other_chapter), ('projectId', 'another-project')):
            reference = put(self.root, 'edit-anchor', {**value, key: other})
            self.assert_rejected({'patches': [row, {**row, 'anchorRef': reference}]}, 'EDIT_ANCHOR_SCOPE_MISMATCH')
        with tempfile.TemporaryDirectory() as temp:
            other = Path(temp).resolve(); review_ready_project(other, chapter_count=1)
            with self.assertRaises(StructureError) as caught:
                patch_candidate(other, patches=[row])
            self.assertEqual(caught.exception.code, 'TRANSPORT_SNAPSHOT_UNAVAILABLE')

    def test_same_body_new_candidate_version_cannot_silently_rebind_old_anchor(self):
        old = self.repair()
        with patch('scripts.core.artifacts.utc_now', return_value='2099-01-01T00:00:00Z'):
            self.install(self.text, [('door', '甲推开门。')])
        self.assert_rejected({'patches': [old]}, 'PATCH_BASE_STALE')
        self.assert_rejected({'patches': [self.repair(), old]}, 'PATCH_BASE_STALE')

    def test_corrupt_or_missing_anchor_and_mismatched_body_or_quote_hash_fail_closed(self):
        row = self.repair(); value = read_anchor(self.root, row['anchorRef'])
        for field, code in (('bodyHash', 'PATCH_BASE_STALE'), ('quoteHash', 'EDIT_ANCHOR_MISMATCH')):
            changed = put(self.root, 'edit-anchor', {**value, field: 'different'})
            self.assert_rejected({'patches': [{**row, 'anchorRef': changed}]}, code)
        path = _directory() / (row['anchorRef'].replace(':', '-') + '.json')
        path.write_bytes(path.read_bytes() + b' ')
        self.assert_rejected({'patches': [row]}, 'TRANSPORT_SNAPSHOT_UNAVAILABLE')
        path.unlink()
        self.assert_rejected({'patches': [row]}, 'TRANSPORT_SNAPSHOT_UNAVAILABLE')

    def test_candidate_file_drift_and_late_guard_drift_preserve_external_changes(self):
        path = self.root / self.base['artifactPath']
        original = path.read_bytes(); changed = original + b'\n'
        path.write_bytes(changed)
        self.assert_rejected({'patches': [self.repair()]}, 'PATCH_BASE_STALE')
        path.write_bytes(original)
        before = self.files()
        changed_during_write = False
        def mutate(phase, relative=None):
            nonlocal changed_during_write
            if phase == 'write' and relative.endswith('.txt') and not changed_during_write:
                changed_during_write = True
                path.write_bytes(changed)
        with patch('scripts.core.write_sets.checkpoint', side_effect=mutate):
            with self.assertRaises(StructureError) as caught:
                patch_candidate(self.root, patches=[self.repair()], requestId='late-change')
        self.assertTrue(changed_during_write)
        self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
        from tests.fixtures_transaction import assert_preserved_rollback
        assert_preserved_rollback(self, self.root, {**before, self.base['artifactPath']: changed}, self.files())

    def test_workflow_change_during_resolution_is_not_overwritten(self):
        from scripts.core.edit_anchors import resolve_anchor
        workflow_path = self.root / '.govel/workflow.json'
        workflow = read_json(workflow_path)
        workflow['externalMarker'] = 'preserve this edit'
        expected = None
        def mutate(*args, **kwargs):
            nonlocal expected
            result = resolve_anchor(*args, **kwargs)
            write_json(workflow_path, workflow)
            expected = workflow_path.read_bytes()
            return result
        before = self.files()
        with patch('scripts.core.candidate_patch.resolve_anchor', side_effect=mutate):
            with self.assertRaises(StructureError) as caught:
                patch_candidate(self.root, patches=[self.repair()])
        self.assertEqual(caught.exception.code, 'PATCH_BASE_STALE')
        self.assertEqual(self.files(), {**before, '.govel/workflow.json': expected})

    def test_anchor_request_resumes_full_diagnostics_without_cache_or_semantic_credit(self):
        row = self.repair()
        arguments = {'patches': [row], 'requestId': 'anchor-retry', 'advanceLifecycle': True,
                     'diagnostics': {'mode': 'plot', 'intensityLevel': 1}}
        approvals = deepcopy(read_json(self.root / '.govel/workflow.json')['approvals'])
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=OSError('injected diagnostics failure')):
            first = call_operation('workflow.patch-candidate', self.root, arguments)
        self.assertEqual(first['code'], 'ARTIFACT_PACK_DIAGNOSTICS_INCOMPLETE')
        self.assertTrue(first['registered']); self.assertFalse(first['approvalPerformed'])
        (_directory() / (row['anchorRef'].replace(':', '-') + '.json')).unlink()
        run = subprocess.run
        with patch('scripts.core.mechanical_evidence.subprocess.run', wraps=run) as called:
            resumed = call_operation('workflow.patch-candidate', self.root, arguments)
        self.assertTrue(resumed['ok'], resumed); self.assertTrue(resumed['replayed'])
        self.assertEqual(first['artifactSha256'], resumed['artifactSha256'])
        self.assertEqual(called.call_count, 1)
        self.assertIn(str(TOOLBOX_ROOT / 'scripts/aggregate_diagnostics.py'), called.call_args.args[0])
        report = get(self.root, resumed['diagnostics']['reportRef'], kind='diagnostics')
        self.assertEqual(set(report['evidence']['checksPresent']),
                         {'ai_cliche_linter', 'validate_text', 'check_voice_lexicon', 'check_degeneration'})
        self.assertFalse(resumed['semanticReviewComplete']); self.assertFalse(resumed['approvalPerformed'])
        self.assertEqual(read_json(self.root / '.govel/workflow.json')['approvals'], approvals)
        self.assertEqual(resumed['lifecycleStatus'], 'review')
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=AssertionError('must reuse')):
            again = call_operation('workflow.patch-candidate', self.root, arguments)
        self.assertTrue(again['diagnostics']['reused'])
        self.assert_rejected({**arguments, 'patches': [{**row, 'replacement': '不同请求'}]}, 'ARTIFACT_REQUEST_REUSED')
        self.install(self.text + '后续版本。', [('door', '甲推开门。')])
        self.assert_rejected(arguments, 'PATCH_BASE_STALE')

    def test_cli_and_mcp_accept_anchor_only_and_share_request_receipt(self):
        arguments = {'patches': [self.repair()], 'requestId': 'cli-anchor'}
        process = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'),
            'project', 'workflow', 'patch-candidate', '--project-root', str(self.root),
            '--patches', json.dumps(arguments['patches'], ensure_ascii=False), '--request-id', arguments['requestId']],
            capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        first = json.loads(process.stdout)
        from scripts.mcp_server import handle
        result = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'patch_candidate_prose', 'arguments': {'projectRoot': str(self.root), **arguments}}})['result']
        self.assertFalse(result['isError'], result)
        self.assertTrue(result['structuredContent']['replayed'])
        self.assertEqual(result['structuredContent']['artifactSha256'], first['artifactSha256'])


if __name__ == '__main__':
    unittest.main()
