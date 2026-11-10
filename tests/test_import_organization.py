"""Real imported prose -> quoted facts -> accepted retrospective history -> continuation."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.import_history import history_status


class ImportOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve(); self.root = self.base / 'book'
        self.enterContext(patch.dict(os.environ, {'GOVEL_LOG_MODE': 'off'}))
        self.source = self.base / '原稿.txt'
        self.source.write_text('（一）交班\n林舟是守门人。林舟和程安是兄弟。钥匙藏在抽屉里。\n（二）夜路\n程安归还了钥匙，林舟接过钥匙。\n')
        p = self.call('import.preview', source=str(self.source))
        self.call('import.commit', planId=p['planId'], planHash=p['planHash'], approve=True)
        self.ids = [r['chapterId'] for r in load_structure(self.root)['chapters']]

    def call(self, op, **args):
        return call_operation(op, self.root, args)

    def content(self, prep, *, accept=True):
        body = deepcopy(prep['fields']); body['acceptHistory'] = accept
        for row in body['chapters']:
            first = row['chapterId'] == self.ids[0]
            quote = '林舟是守门人。' if first else '程安归还了钥匙，林舟接过钥匙。'
            row.update(synopsis='林舟守门，兄弟关系与钥匙位置在此交代。' if first else '程安把钥匙交还林舟，归还动作完成。',
                       quotes=[{'text': quote}], review={'facts': '核对本章角色身份和钥匙状态，提取结果与原句对应。',
                       'relationships': '按原文检查人物关系，未把动作先后误当成关系变化。',
                       'structure': '本批为同一归还事件的两章，沿用一卷一弧的排列，回溯梗概准确覆盖已写事件。',
                       'continuity': '本章离场状态可由所列原句核对；不存在未解决的交接矛盾。'})
        if self.ids[0] in prep['selectedChapterIds']:
            body['entities'] = [
                {'ref': '$a', 'entityType': 'character', 'name': '林舟', 'summary': '林舟是守门人。', 'chapterId': self.ids[0], 'quote': {'text': '林舟是守门人。'}},
                {'ref': '$b', 'entityType': 'character', 'name': '程安', 'summary': '程安与林舟是兄弟。', 'chapterId': self.ids[0], 'quote': {'text': '林舟和程安是兄弟。'}}]
            body['relations'] = [{'fromRef': '$a', 'toRef': '$b', 'kind': '兄弟', 'chapterId': self.ids[0],
                                  'quote': {'text': '林舟和程安是兄弟。'}, 'reason': '关系由原文明示。'}]
            body['facts'] = [{'ownerRef': '$a', 'text': '林舟是守门人。', 'chapterId': self.ids[0], 'quote': {'text': '林舟是守门人。'}}]
        return body

    def prepare(self, **args):
        return self.call('import.organization-prepare', **args)

    def preview(self, prep, body, request='organize-1'):
        return self.call('import.organization-preview', prepareRef=prep['prepareRef'], requestId=request, content=body)

    def commit(self, plan):
        return self.call('source.commit', operationId=plan['operationId'], planHash=plan['planHash'], approve=True)

    def workflow(self):
        return json.loads((self.root / '.govel/workflow.json').read_text())

    def test_full_acceptance_preserves_prose_records_evidence_and_allows_next_chapter(self):
        original = {c['filePath']: (self.root / c['filePath']).read_bytes() for c in load_structure(self.root)['chapters']}
        prep = self.prepare(); body = self.content(prep)
        plan = self.preview(prep, body)
        self.assertTrue(all(c['status'] == 'planning_required' for c in load_structure(self.root)['chapters']))
        self.commit(plan)
        source = load_structure(self.root); workflow = self.workflow()
        self.assertEqual(len(source['entities']), 2)
        self.assertEqual(source['relations'][0]['kind'], '兄弟')
        self.assertEqual(workflow['approvals'], {})
        self.assertEqual(workflow['artifacts'], {})
        self.assertEqual(workflow['approvedChapterIds'], [])
        for chapter in source['chapters']:
            self.assertEqual(chapter['status'], 'stable')
            self.assertTrue(history_status(self.root, source, workflow, chapter)['complete'])
            self.assertEqual((self.root / chapter['filePath']).read_bytes(), original[chapter['filePath']])
        self.assertTrue(all(f['contentRole'] == 'fact' for f in source['fragments']))
        citations = self.call('source.evidence')
        self.assertTrue(citations)
        from scripts.core.continuity import require_predecessors_ready, finalization_evidence
        require_predecessors_ready(source, workflow, {'chapterId': 'future', 'displayOrdinal': 3}, root=self.root)
        self.assertEqual(finalization_evidence(self.root, source, workflow, source['chapters'][0])['kind'], 'accepted_import_review')
        self.assertEqual(self.prepare()['code'], 'IMPORT_ORGANIZATION_NO_PENDING')
        summary = self.call('summary.prepare')
        self.assertIn('prepareRef', summary)
        self.call('summary.commit-delta', prepareRef=summary['prepareRef'], storySummary='林舟守门，程安将钥匙交回林舟。')
        from scripts.core.work_voice_profile import AXES
        profile = self.call('source.voice-profile-preview', action='set', expectedRevision=source['revision'],
            reason='由中性原稿提炼叙述方式', axes={key: '简短动作句，按场景中可见行为叙述。' for key in AXES},
            examples=['林舟是守门人。'], applicability='后续章节沿用原稿的动作叙述方式。')
        self.commit(profile)
        source = load_structure(self.root)
        profile = self.call('source.voice-profile-preview', action='approve', expectedRevision=source['revision'],
            reason='测试中的显式作者采用', approvalActor='测试作者', approvalEvidence='确认本次已展示的风格候选')
        self.commit(profile)
        status = self.call('import.organization-status')
        self.assertEqual(next(r for r in status['tasks'] if r['id'] == 'voice_profile')['status'], 'active')
        from scripts.core.project_doctor import project_doctor
        report = project_doctor(self.root)
        self.assertFalse([row for row in report['issues'] if row['domain'] == 'workflow' and row['severity'] == 'blocking'], report['issues'])

    def test_unresolved_or_false_quotes_cannot_be_accepted(self):
        prep = self.prepare(); body = self.content(prep)
        body['chapters'][0]['unresolved'] = ['钥匙来源仍需作者确认']
        with self.assertRaises(StructureError) as exc: self.preview(prep, body)
        self.assertEqual(exc.exception.code, 'IMPORT_REVIEW_UNRESOLVED')
        body['chapters'][0]['unresolved'] = []
        body['facts'][0]['quote'] = {'text': '不存在的原句'}
        with self.assertRaises(StructureError): self.preview(prep, body)
        self.assertEqual(self.workflow()['approvals'], {})

    def test_missing_origin_refuses_generic_current_project(self):
        (self.root / '.govel/import-receipt.json').unlink()
        with self.assertRaises(StructureError) as exc: self.prepare()
        self.assertEqual(exc.exception.code, 'IMPORT_ORIGIN_REQUIRED')

    def test_empty_selection_and_bad_origin_never_claim_completed_review(self):
        with self.assertRaises(StructureError): self.prepare(chapterIds=[])
        page = self.prepare(offset=len(self.ids))
        self.assertEqual(page['code'], 'IMPORT_ORGANIZATION_PAGE_COMPLETE')
        self.assertEqual(page['remaining'], len(self.ids))
        path = self.root / '.govel/import-receipt.json'
        receipt = json.loads(path.read_text()); receipt['planHash'] = 'invalid'
        path.write_text(json.dumps(receipt))
        with self.assertRaises(StructureError): self.prepare()

    def test_source_edit_invalidates_prepared_reads_and_commit(self):
        prep = self.prepare(); plan = self.preview(prep, self.content(prep))
        chapter = load_structure(self.root)['chapters'][0]
        path = self.root / chapter['filePath']; path.write_text('外部修改保留')
        with self.assertRaises(StructureError): self.call('import.organization-read', prepareRef=prep['prepareRef'])
        with self.assertRaises(StructureError): self.commit(plan)
        self.assertEqual(path.read_text(), '外部修改保留')
        self.assertFalse(self.workflow().get('importReviews'))

    def test_unapproved_preview_cannot_commit_and_request_replay_does_not_duplicate(self):
        prep = self.prepare(); body = self.content(prep); plan = self.preview(prep, body)
        with self.assertRaises(StructureError):
            self.call('source.commit', operationId=plan['operationId'], planHash=plan['planHash'], approve=False)
        self.commit(plan); before = (self.root / '.govel/structure.json').read_bytes()
        replay = self.preview(prep, body)
        self.assertTrue(replay['alreadyCommitted'])
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)

    def test_preview_without_acceptance_keeps_chapters_unready(self):
        prep = self.prepare(); self.commit(self.preview(prep, self.content(prep, accept=False)))
        self.assertTrue(all(c['status'] == 'planning_required' for c in load_structure(self.root)['chapters']))
        self.assertFalse(self.workflow().get('importReviews'))
        # Same synopsis may be explicitly reviewed again without duplicate facts.
        second = self.prepare(); body = self.content(second)
        body['entities'] = []; body['relations'] = []; body['facts'] = []
        existing = load_structure(self.root)
        body['chapters'][0]['existingRecordIds'] = [r['entityId'] for r in existing['entities']] + [r['relationId'] for r in existing['relations']]
        self.commit(self.preview(second, body, 'accept-reviewed'))
        self.assertTrue(all(c['status'] == 'stable' for c in load_structure(self.root)['chapters']))

    def test_batches_preserve_prior_accepted_work(self):
        first = self.prepare(limit=1); self.commit(self.preview(first, self.content(first), 'batch-1'))
        second = self.prepare(limit=1)
        self.assertEqual(second['selectedChapterIds'], [self.ids[1]])
        self.commit(self.preview(second, self.content(second), 'batch-2'))
        source = load_structure(self.root)
        self.assertTrue(all(history_status(self.root, source, self.workflow(), c)['complete'] for c in source['chapters']))

    def test_commit_fault_restores_all_semantic_outputs_and_allows_retry(self):
        prep = self.prepare(); plan = self.preview(prep, self.content(prep))
        paths = ['.govel/structure.json', '.govel/workflow.json', '.govel/source-prose-evidence.json']
        before = {p: (self.root / p).read_bytes() if (self.root / p).exists() else None for p in paths}
        def fail(phase, path=None):
            if phase == 'write' and path == '.govel/structure.json': raise RuntimeError('injected interruption')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(RuntimeError): self.commit(plan)
        self.assertEqual({p: (self.root / p).read_bytes() if (self.root / p).exists() else None for p in paths}, before)
        self.commit(plan)
        self.assertEqual(len(self.workflow()['importReviews']), 2)

    def test_cli_and_mcp_expose_same_organization_path(self):
        import subprocess, sys
        from scripts.mcp_server import handle
        from scripts.core.project import TOOLBOX_ROOT
        process = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'import',
            'organization-prepare', '--project-root', str(self.root), '--limit', '1'], text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        prep = json.loads(process.stdout); body = self.content(prep)
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'preview_import_organization', 'arguments': {'projectRoot': str(self.root),
                'prepareRef': prep['prepareRef'], 'requestId': 'mcp-organization', 'content': body}}})['result']
        self.assertFalse(response['isError'], response)
        plan = response['structuredContent']
        self.commit(plan)
        self.assertEqual(len(self.workflow()['importReviews']), 1)

    def test_missing_evidence_or_committed_image_revokes_readiness(self):
        prep = self.prepare(); self.commit(self.preview(prep, self.content(prep)))
        source = load_structure(self.root); workflow = self.workflow()
        evidence = self.root / '.govel/source-prose-evidence.json'
        saved = evidence.read_bytes(); evidence.write_text('{"batches":{}}')
        self.assertFalse(history_status(self.root, source, workflow, source['chapters'][0])['complete'])
        evidence.write_bytes(saved)
        operation_id = workflow['importReviews'][self.ids[0]]['operationId']
        (self.root / '.govel/operations' / (operation_id + '.json')).unlink()
        self.assertFalse(history_status(self.root, source, workflow, source['chapters'][0])['complete'])

    def test_changed_fact_cannot_reuse_accepted_history(self):
        prep = self.prepare(); self.commit(self.preview(prep, self.content(prep)))
        source = load_structure(self.root); workflow = self.workflow()
        source['entities'][0]['summary'] = '与原审阅不同的身份'
        self.assertFalse(history_status(self.root, source, workflow, source['chapters'][0])['complete'])

    def test_guard_removal_and_extra_write_cannot_be_smuggled_into_plan(self):
        from scripts.core.structure_commands import _operation_paths, _plan_hash
        from scripts.core.jsonio import read_json, write_json
        prep = self.prepare(); report = self.preview(prep, self.content(prep))
        path, _, _ = _operation_paths(self.root, report['operationId'])
        original = read_json(path)
        for field in ('organizationGuards', 'importOrganization'):
            plan = deepcopy(original); plan.pop(field)
            plan['planHash'] = _plan_hash({k: v for k, v in plan.items() if k != 'planHash'})
            write_json(path, plan)
            with self.assertRaises(StructureError): self.commit(plan)
            self.assertFalse(self.workflow().get('importReviews'))
        write_json(path, original)
        self.commit(original)

    def test_compacted_preview_retains_paginated_review_conclusions(self):
        prep = self.prepare(); body = self.content(prep)
        conclusion = '这是对钥匙状态的实际审阅说明。' * 900
        body['chapters'][0]['review']['facts'] = conclusion
        plan = self.preview(prep, body)
        self.assertTrue(plan['responseCompacted'])
        action = plan['reviewAction']; arguments = dict(action['arguments'])
        text = ''
        while True:
            page = self.call(action['operation'], **arguments)
            text += ''.join(row['text'] for row in page['items'])
            if not page['hasMore']: break
            arguments['cursor'] = page['nextCursor']
        self.assertIn(conclusion, text)
        self.assertIn('acceptedHistoryRequested', text)
        self.assertIn('historicalStageAClaimed', text)

    def test_verify_detects_full_coverage_and_external_source_change_separately(self):
        result = self.call('import.verify', checkSource=True)
        self.assertTrue(result['allImportedTextPreserved'])
        self.source.write_text('原文件后来变了')
        changed = self.call('import.verify', checkSource=True)
        self.assertTrue(changed['allImportedTextPreserved'])
        self.assertEqual(changed['files'][0]['externalSourceStatus'], 'changed')
        source = load_structure(self.root)
        (self.root / source['chapters'][0]['filePath']).write_text('正文被外部改动')
        damaged = self.call('import.verify')
        self.assertFalse(damaged['ok'])

    def test_verify_refuses_missing_copies_and_receipt_range_tampering(self):
        copy = self.root / '.govel/import-source/sources/0.bin'
        saved = copy.read_bytes(); copy.write_text('changed')
        self.assertFalse(self.call('import.verify')['ok'])
        copy.write_bytes(saved)
        receipt_path = self.root / '.govel/import-receipt.json'
        receipt = json.loads(receipt_path.read_text()); receipt['chapters'][0]['utf8ByteStart'] += 1
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaises(StructureError): self.call('import.verify')

    def test_verify_refuses_change_after_a_chapter_was_checked(self):
        from scripts.core.import_verification import validate_guards
        chapter = load_structure(self.root)['chapters'][0]
        def mutate(root, guards, *, stage):
            (root / chapter['filePath']).write_text('较早检查过的正文后来变化')
            return validate_guards(root, guards, stage=stage)
        with patch('scripts.core.import_verification.validate_guards', side_effect=mutate):
            with self.assertRaises(StructureError): self.call('import.verify')

    def test_doctor_and_live_followup_share_pending_definitions(self):
        from scripts.core.project_doctor import project_doctor
        followup = self.call('import.organization-status')
        self.assertEqual(followup, project_doctor(self.root)['importFollowup'])
        self.assertFalse(followup['planningMarkdownRequired'])
        self.assertTrue(all('cli' in row['action'] for row in followup['tasks']))

    def test_paged_read_roundtrip_and_stale_source(self):
        prep = self.prepare(); texts = {}
        args = {'prepareRef': prep['prepareRef'], 'limit': 1}
        while True:
            page = self.call('import.organization-read', **args)
            for row in page['materials']: texts[row['id']] = texts.get(row['id'], '') + row['text']
            if not page['hasMore']: break
            args['cursor'] = page['nextCursor']
        self.assertEqual(set(texts), set(self.ids))
        self.assertEqual(''.join(texts[cid] for cid in self.ids), self.source.read_text())

    def test_doctor_reports_data_health_separately_from_authoring(self):
        from scripts.core.project_doctor import project_doctor
        report = project_doctor(self.root)
        self.assertTrue(report['dataIntegrity']['healthy'])
        self.assertFalse(report['authoringReadiness']['ready'])
        self.assertEqual(report['assessment'], 'authoring_pending')
        self.assertIn('导入失败', report['message'])


if __name__ == '__main__': unittest.main()
