"""Automatic repair accounting commits with the candidate and resumes from receipts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import artifact_request_path, pack_artifact
from scripts.core.authoring_submission import submit_artifacts
from scripts.core.candidate_patch import patch_candidate
from scripts.core.jsonio import read_json, write_json
from scripts.core.project import repair_policy
from scripts.core.structure_store import StructureError
from scripts.core.workflow import WorkflowError
from tests.fixtures_workflow import review_ready_project


BODY = '他把钥匙放回桌上，合上了登记本。'
REVISED = '他把钥匙放回桌上，核对签名后合上了登记本。'


class RepairAccountingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def workflow(self):
        return read_json(self.root / '.govel/workflow.json')

    def inventory(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def count(self):
        return self.workflow()['repairRounds'].get(self.cid, 0)

    def set_count(self, count):
        workflow = self.workflow()
        workflow['repairRounds'][self.cid] = count
        write_json(self.root / '.govel/workflow.json', workflow)

    def pack(self, **kwargs):
        return pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose',
                             text=REVISED, requestId='repair-pack', **kwargs)

    def patch_args(self):
        return {'chapterId': self.cid,
                'candidateSha256': self.workflow()['artifacts'][self.cid]['candidate_prose']['artifactSha256'],
                'requestId': 'repair-patch', 'automaticRepair': True,
                'patches': [{'issueId': 'signature', 'original': {'text': '合上了登记本'}, 'replacement': '核对签名后合上了登记本'}]}

    def submit_args(self):
        return {'requestId': 'repair-submit', 'chapterId': self.cid, 'automaticRepair': True,
                'artifacts': [{'artifactType': 'candidate_prose', 'text': REVISED}]}

    def test_regular_candidate_and_patch_never_increment_without_explicit_intent(self):
        result = self.pack()
        self.assertNotIn('repairAccounting', result)
        args = self.patch_args(); args.pop('automaticRepair')
        args['patches'][0].update(original={'text': '核对签名后合上了登记本'}, replacement='收起了登记本')
        result = patch_candidate(self.root, **args)
        self.assertNotIn('repairAccounting', result)
        args = self.submit_args(); args.pop('automaticRepair')
        result = submit_artifacts(self.root, **args)
        self.assertTrue(result['ok'], result)
        self.assertNotIn('repairAccounting', result)
        self.assertEqual(self.workflow()['repairRounds'], {})

    def test_explicit_repair_requires_boolean_candidate_and_request_id(self):
        before = self.inventory()
        invalid = [
            {'artifactType': 'candidate_prose', 'text': REVISED, 'automaticRepair': True},
            {'artifactType': 'candidate_prose', 'text': REVISED, 'automaticRepair': True, 'requestId': ''},
            {'artifactType': 'candidate_prose', 'text': REVISED, 'automaticRepair': 'true', 'requestId': 'bad'},
            {'artifactType': 'title_candidates', 'payload': {'candidates': [{'title': '归还'}]},
             'automaticRepair': True, 'requestId': 'bad'},
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(StructureError):
                pack_artifact(self.root, chapterId=self.cid, **args)
            self.assertEqual(self.inventory(), before)
        args = self.patch_args(); args.pop('requestId')
        with self.assertRaises(StructureError) as caught:
            patch_candidate(self.root, **args)
        self.assertEqual(caught.exception.code, 'AUTOMATIC_REPAIR_REQUEST_REQUIRED')
        with self.assertRaises(StructureError):
            submit_artifacts(self.root, requestId='not-a-candidate', chapterId=self.cid,
                             artifacts=[{'artifactType': 'state_candidate', 'payload': {'changes': []}}],
                             automaticRepair=True)
        self.assertEqual(self.inventory(), before)

    def test_success_count_and_request_receipt_are_in_the_candidate_write_set(self):
        from scripts.core import artifact_pack as module
        real_apply = module.apply_write_set
        observed = []

        def inspect(root, outputs, **kwargs):
            staged_workflow = json.loads(outputs['.govel/workflow.json'])
            request_path = artifact_request_path(read_json(root / '.govel/structure.json')['projectId'], 'repair-pack')
            request = json.loads(outputs[request_path])
            self.assertEqual(staged_workflow['repairRounds'][self.cid], 1)
            self.assertIn(request['receipt']['artifactPath'], outputs)
            self.assertEqual(request['receipt']['repairAccounting'],
                             {'requestId': 'repair-pack', 'previousRound': None, 'round': 1})
            observed.append(request)
            return real_apply(root, outputs, **kwargs)

        approvals = deepcopy(self.workflow()['approvals'])
        with patch.object(module, 'apply_write_set', side_effect=inspect):
            result = self.pack(automaticRepair=True)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.count(), 1)
        self.assertEqual(result['repairAccounting']['round'], 1)
        self.assertEqual(self.workflow()['approvals'], approvals)

    def test_retry_is_read_only_even_when_the_repair_limit_is_now_reached(self):
        maximum = repair_policy()['maxAutomaticRounds']
        self.set_count(maximum - 1)
        first = self.pack(automaticRepair=True)
        before = self.inventory()
        replay = self.pack(automaticRepair=True)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['artifactSha256'], first['artifactSha256'])
        self.assertEqual(replay['repairAccounting']['round'], maximum)
        self.assertEqual(self.inventory(), before)

    def test_cap_rejects_pack_and_patch_before_any_candidate_write(self):
        self.set_count(repair_policy()['maxAutomaticRounds'])
        args = self.patch_args()
        before = self.inventory()
        for call in (lambda: self.pack(automaticRepair=True), lambda: patch_candidate(self.root, **args)):
            with self.assertRaises(WorkflowError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'REPAIR_ROUND_LIMIT')
            self.assertEqual(self.inventory(), before)

    def test_validation_and_transaction_failures_never_consume_a_round(self):
        before = self.inventory()
        with self.assertRaises(StructureError):
            pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text='',
                          requestId='invalid', automaticRepair=True)
        self.assertEqual(self.inventory(), before)

        def interrupt(phase, relative=None):
            if phase == 'write' and relative == '.govel/workflow.json':
                raise RuntimeError('interrupt after staging the counter and candidate')

        with patch('scripts.core.write_sets.checkpoint', side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                self.pack(automaticRepair=True)
        self.assertEqual(self.count(), 0)
        after = self.inventory()
        added = set(after) - set(before)
        completed = [name for name in added if name.endswith('/completed.json')]
        self.assertEqual(len(completed), 1, added)
        terminal = json.loads(after[completed[0]])
        self.assertEqual(terminal['phase'], 'rolled_back')
        self.assertEqual(terminal['publicationProtocol'], 'preserve-exclusive-v1')
        prefix = '.govel/transactions/' + terminal['transactionId'] + '/'
        self.assertTrue(all(name.startswith(prefix) for name in added), added)
        self.assertEqual({key: value for key, value in after.items() if key not in added}, before)
        self.assertFalse((self.root / '.govel/transactions' / (terminal['transactionId'] + '.json')).exists())
        for row in terminal['files']:
            if row['before']['exists']:
                self.assertEqual(after[row['path']], before[row['path']])
                self.assertEqual(after[prefix + 'before/' + row['path']], before[row['path']])
            else:
                self.assertNotIn(row['path'], after)
            if row['after']['exists']:
                self.assertEqual(hashlib.sha256(after[prefix + 'after/' + row['path']]).hexdigest(),
                                 row['after']['sha256'])
        self.assertTrue(self.pack(automaticRepair=True)['registered'])
        self.assertEqual(self.count(), 1)

    def test_diagnostics_failure_keeps_the_committed_count_and_patch_resumes_once(self):
        args = self.patch_args()
        args.update(advanceLifecycle=True, diagnostics={'mode': 'plot', 'intensityLevel': 1})
        with patch('scripts.core.mechanical_evidence.run_diagnostics', side_effect=OSError('diagnostics unavailable')):
            partial = patch_candidate(self.root, **args)
        self.assertFalse(partial['ok'])
        self.assertTrue(partial['registered'])
        self.assertEqual(self.count(), 1)
        self.assertEqual(partial['repairAccounting']['round'], 1)
        replay = patch_candidate(self.root, requestId='repair-patch')
        self.assertTrue(replay['ok'], replay)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['artifactSha256'], partial['artifactSha256'])
        self.assertEqual(self.count(), 1)

    def test_request_cannot_change_automatic_repair_intent(self):
        self.pack(automaticRepair=True)
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.pack()
        self.assertEqual(caught.exception.code, 'ARTIFACT_REQUEST_REUSED')
        self.assertEqual(self.inventory(), before)
        args = self.patch_args(); args['requestId'] = 'another-patch'
        args['patches'][0].update(original={'text': '核对签名后合上了登记本'}, replacement='收起了登记本')
        patch_candidate(self.root, **args)
        before = self.inventory()
        args['automaticRepair'] = False
        with self.assertRaises(StructureError) as caught:
            patch_candidate(self.root, **args)
        self.assertEqual(caught.exception.code, 'ARTIFACT_REQUEST_REUSED')
        self.assertEqual(self.inventory(), before)

    def test_complete_rewrite_counts_once_and_resumes_using_only_request_id(self):
        self.set_count(repair_policy()['maxAutomaticRounds'] - 1)
        result = submit_artifacts(self.root, **self.submit_args())
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.count(), repair_policy()['maxAutomaticRounds'])
        self.assertEqual(result['repairAccounting']['round'], repair_policy()['maxAutomaticRounds'])
        before = self.inventory()
        replay = submit_artifacts(self.root, requestId='repair-submit')
        self.assertTrue(replay['ok'], replay)
        self.assertTrue(replay['results'][0]['replayed'])
        self.assertEqual(self.inventory(), before)
        args = self.submit_args(); args['automaticRepair'] = False
        with self.assertRaises(StructureError) as caught:
            submit_artifacts(self.root, **args)
        self.assertEqual(caught.exception.code, 'SUBMISSION_REQUEST_REUSED')
        self.assertEqual(self.inventory(), before)

    def test_submit_counts_before_diagnostics_and_accepts_only_its_own_increment(self):
        args = self.submit_args()
        args.update(advanceLifecycle=True, diagnostics={'mode': 'plot', 'intensityLevel': 1})
        with patch('scripts.core.mechanical_evidence.run_diagnostics', side_effect=OSError('temporary diagnostics error')):
            partial = submit_artifacts(self.root, **args)
        self.assertFalse(partial['ok'])
        self.assertEqual(partial['stages'][0]['status'], 'registered')
        self.assertEqual(self.count(), 1)
        self.assertEqual(partial['repairAccounting']['round'], 1)
        restored = submit_artifacts(self.root, requestId='repair-submit')
        self.assertTrue(restored['ok'], restored)
        self.assertEqual(self.count(), 1)

    def test_count_receipt_survives_large_result_compaction(self):
        result = self.pack(automaticRepair=True, writingNotes={'deviations': [
            {'id': 'D1', 'kind': 'intensityLevel', 'intensityLevel': 1, 'reason': '作者说明本轮呈现取舍。' * 2000}]})
        self.assertTrue(result['responseCompacted'])
        self.assertEqual(result['repairAccounting']['round'], 1)
        self.assertEqual(self.count(), 1)
        self.assertIn('readAction', result)

    def test_rewrite_and_following_artifact_share_the_same_counted_submission(self):
        args = self.submit_args()
        args['artifacts'].append({'artifactType': 'title_candidates', 'payload': {
            'candidates': [{'title': '签名', 'reason': '本章修订动作'}]}})
        result = submit_artifacts(self.root, **args)
        self.assertTrue(result['ok'], result)
        self.assertEqual([stage['status'] for stage in result['stages']], ['registered', 'registered'])
        self.assertEqual(self.count(), 1)
        before = self.inventory()
        replay = submit_artifacts(self.root, requestId='repair-submit')
        self.assertTrue(replay['ok'], replay)
        self.assertEqual(self.inventory(), before)

    def test_lost_response_after_commit_resumes_without_another_round(self):
        def interrupt(root, **kwargs):
            pack_artifact(root, **kwargs)
            raise RuntimeError('response lost after commit')

        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=interrupt):
            partial = submit_artifacts(self.root, **self.submit_args())
        self.assertFalse(partial['ok'])
        self.assertEqual(partial['stages'][0]['status'], 'registered')
        self.assertEqual(self.count(), 1)
        before = self.inventory()
        replay = submit_artifacts(self.root, requestId='repair-submit')
        self.assertTrue(replay['ok'], replay)
        self.assertEqual(self.inventory(), before)

    def test_submit_failure_before_candidate_and_cap_do_not_consume_rounds(self):
        args = self.submit_args()
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=OSError('candidate not saved')):
            partial = submit_artifacts(self.root, **args)
        self.assertFalse(partial['ok'])
        self.assertEqual(partial['stages'][0]['status'], 'pending')
        self.assertEqual(self.count(), 0)
        self.assertTrue(submit_artifacts(self.root, requestId='repair-submit')['ok'])
        self.assertEqual(self.count(), 1)
        self.set_count(repair_policy()['maxAutomaticRounds'])
        before = self.workflow()
        args['requestId'] = 'exhausted'; args['artifacts'][0]['text'] += '随后他转身离开。'
        rejected = submit_artifacts(self.root, **args)
        self.assertFalse(rejected['ok'])
        self.assertEqual(rejected['error']['code'], 'REPAIR_ROUND_LIMIT')
        self.assertEqual(rejected['stages'][0]['status'], 'pending')
        self.assertEqual(self.workflow(), before)

    def test_external_round_or_approval_change_still_blocks_submission_resume(self):
        result = submit_artifacts(self.root, **self.submit_args())
        self.assertTrue(result['ok'], result)
        base = self.workflow()
        for name in ('same chapter round', 'another chapter round', 'approval scope'):
            workflow = deepcopy(base)
            if name == 'same chapter round':
                workflow['repairRounds'][self.cid] += 1
            elif name == 'another chapter round':
                workflow['repairRounds']['CH-ANOTHER'] = 1
            else:
                workflow['approvedChapterIds'] = []
            write_json(self.root / '.govel/workflow.json', workflow)
            before = self.inventory()
            with self.subTest(change=name):
                replay = submit_artifacts(self.root, requestId='repair-submit')
                self.assertFalse(replay['ok'])
                self.assertEqual(replay['error']['code'], 'SUBMISSION_INPUTS_STALE')
                self.assertEqual(self.inventory(), before)

    def test_workflow_race_at_publication_cannot_overwrite_external_round(self):
        def race(phase, relative=None):
            if phase == 'prepared':
                self.set_count(1)

        before = self.workflow()['artifacts'][self.cid]['candidate_prose']
        with patch('scripts.core.write_sets.checkpoint', side_effect=race):
            with self.assertRaises((ValueError, OSError)):
                self.pack(automaticRepair=True)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.workflow()['artifacts'][self.cid]['candidate_prose'], before)
        source = read_json(self.root / '.govel/structure.json')
        self.assertFalse((self.root / artifact_request_path(source['projectId'], 'repair-pack')).exists())


if __name__ == '__main__':
    unittest.main()
