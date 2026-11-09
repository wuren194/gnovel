"""Incremental transport, proposal convenience and candidate automation boundaries."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.operations import call_operation
from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import sha256_text
from scripts.core.jsonio import read_json, write_json
from scripts.core.reading_batch import read_batch
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.project import TOOLBOX_ROOT, toolbox_manifest, chapter_length_bounds
from tests.fixtures_workflow import review_ready_project
from tests.fixtures_source import source_fixture


class IncrementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = review_ready_project(self.root, chapter_count=1)
        self.chapter = self.fixture['chapterIds'][0]

    def prepare(self, task='editor'):
        return call_operation('task.prepare', self.root, {'task': task, 'chapterId': self.chapter, 'view': 'model'})

    def test_manifest_core_references_are_active_for_every_declared_station(self):
        from scripts.core.knowledge_store import _parse
        for station, refs in toolbox_manifest()['onDemandReferences'].items():
            if station.startswith('_'): continue
            for alias in refs:
                path = (TOOLBOX_ROOT / alias).resolve()
                if path.is_file() and path.is_relative_to(TOOLBOX_ROOT / 'knowledge/assets'):
                    meta, _ = _parse(path.read_bytes(), str(path))
                    self.assertEqual(meta['status'], 'active', alias)
                    self.assertIn(station.removeprefix('govel-'), meta['stations'], alias)

    def test_shared_reference_is_readable_for_planner_and_writer(self):
        for task, suffix in [('planner', 'chapter-outline-review-template.md'), ('writer', 'mechanical-baseline.md')]:
            report = self.prepare(task)
            ref = next(r for r in report['reading']['referenceReads']
                       if r.get('referencePath', r['path']).endswith(suffix))
            result = read_batch(self.root, credential=report['reading']['credential'], readIds=[ref['readId']])
            self.assertTrue(result['items'][0]['text'])

    def test_receipt_omits_unchanged_text_without_semantic_credit(self):
        report = self.prepare()
        args = {'credential': report['reading']['credential'], 'readIds': report['contractReads'][0]['readIds']}
        first = read_batch(self.root, **args)
        while first['hasMore']:
            first = read_batch(self.root, **args, cursor=first['nextCursor'])
        second = read_batch(self.root, **args, priorReceipt=first['sessionReadReceipt'])
        self.assertEqual(second['items'], [])
        self.assertGreater(second['unchangedSummary']['chunks'], 0)
        self.assertFalse(second['hasMore'])
        self.assertFalse(second['coverage']['semanticReviewComplete'])
        self.assertLess(len(json.dumps(second)), len(json.dumps(read_batch(self.root, **args))))
        with self.assertRaises(StructureError):
            read_batch(self.root, **args, priorReceipt={'broken': True})

    def test_partial_delivery_never_omits_undelivered_tail_and_file_change_resends(self):
        report = self.prepare()
        row = next(r for r in report['readList'] if r.get('readId') and r.get('path'))
        path = self.root / row['path']; path.write_text('甲' * 15000)
        report = self.prepare(); row = next(r for r in report['readList'] if r.get('path') == row['path'])
        args = {'credential': report['reading']['credential'], 'readIds': [row['readId']], 'maxBytes': 6000}
        first = read_batch(self.root, **args)
        self.assertTrue(first['hasMore'])
        second = read_batch(self.root, **args, priorReceipt=first['sessionReadReceipt'])
        self.assertIn('text', second['items'][0])
        last = first
        while last['hasMore']: last = read_batch(self.root, **args, cursor=last['nextCursor'])
        complete = read_batch(self.root, **args, priorReceipt=last['sessionReadReceipt'])
        self.assertEqual(complete['items'], [])
        self.assertEqual(complete['unchangedSummary']['chunks'], 1)
        path.write_text('乙' + path.read_text()[1:])
        fresh = self.prepare(); freshrow = next(r for r in fresh['readList'] if r.get('path') == row['path'])
        resend = read_batch(self.root, credential=fresh['reading']['credential'], readIds=[freshrow['readId']], priorReceipt=last['sessionReadReceipt'])
        self.assertTrue(resend['items'][0]['text'].startswith('乙'))

    def test_source_contract_budget_and_pov_resolution(self):
        source = load_structure(self.root)
        character = next(r['entityId'] for r in source['entities'] if r['entityType'] == 'character')
        low, high = chapter_length_bounds()
        plan = next(f for f in source['fragments'] if f['ownerId'] == self.chapter)
        plan['chapterContract'] = {'targetRange': [low, high], 'pov': character, 'mode': 'plot', 'intensityLevel': 1,
                                   'beats': [{'id': 'B01', 'budget': low}], 'slowdownReserve': '取钥匙的阻力可展开'}
        write_json(self.root/'.govel/structure.json', source)
        facts = call_operation('task.outline-check', self.root, {'chapterId': self.chapter})
        self.assertTrue(facts['valid'], facts)
        self.assertEqual(facts['beatBudgetSum'], low)
        prepared = self.prepare('planner')
        self.assertEqual(prepared['outlineFacts']['pov'], character)
        self.assertEqual(prepared['outlineFacts']['intensityLevel'], 1)
        plan['chapterContract']['beats'][0]['budget'] = 1
        write_json(self.root/'.govel/structure.json', source)
        bad = call_operation('task.outline-check', self.root, {'chapterId': self.chapter})
        self.assertIn('OUTLINE_BEAT_BUDGET_OUTSIDE_RANGE', bad['issues'])

    def test_skeleton_does_not_approve_or_replace_existing_files(self):
        from scripts.core.review_artifacts import STAGE_B_SLOTS, AUXILIARY_GATES
        before = (self.root / '.govel/workflow.json').read_bytes()
        for kind in ('stage_a_review', 'stage_b_review'):
            result = call_operation('workflow.review-skeleton', self.root, {'chapterId': self.chapter, 'artifactType': kind, 'output': f'工作台/{kind}-input.json'})
            payload = read_json(self.root / result['path'])
            self.assertNotIn('outlineSha256', payload)
            if kind == 'stage_b_review':
                self.assertEqual(set(payload['slots']), set(STAGE_B_SLOTS) | set(AUXILIARY_GATES))
                self.assertTrue(all(v['conclusion'] == '' for v in payload['slots'].values()))
            with self.assertRaises(StructureError):
                call_operation('workflow.review-skeleton', self.root, {'chapterId': self.chapter, 'artifactType': kind, 'output': result['path']})
        self.assertEqual(before, (self.root / '.govel/workflow.json').read_bytes())

    def test_skeleton_does_not_overwrite_a_file_created_during_preparation(self):
        from scripts.core import write_sets
        original = write_sets.inventory
        output = self.root / '工作台/review-input.json'
        before = (self.root / '.govel/workflow.json').read_bytes()

        def create_external(root, outputs, **kwargs):
            output.write_text('authored while preparing')
            return original(root, outputs, **kwargs)

        with patch.object(write_sets, 'inventory', side_effect=create_external), self.assertRaises(StructureError) as caught:
            call_operation('workflow.review-skeleton', self.root, {
                'chapterId': self.chapter, 'artifactType': 'stage_b_review', 'output': '工作台/review-input.json'})
        self.assertEqual(caught.exception.code, 'ARTIFACT_INPUT_OVERWRITE')
        self.assertEqual(output.read_text(), 'authored while preparing')
        self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), before)

    def test_candidate_diagnostics_advances_only_to_review_and_evidence_is_fresh(self):
        from scripts.core.mechanical_evidence import evidence_current
        source = load_structure(self.root); source['chapters'][0]['status'] = 'planned'
        write_json(self.root / '.govel/structure.json', source)
        (self.root / '工作台/input.txt').write_text('甲推开了门。乙退开半步。显然，他还想开口。')
        result = call_operation('workflow.artifact-pack', self.root, {'chapterId': self.chapter,
            'artifactType': 'candidate_prose', 'input': '工作台/input.txt', 'advanceLifecycle': True,
            'diagnostics': {'mode': 'plot', 'intensityLevel': 1}})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['lifecycleStatus'], 'review')
        self.assertFalse(result['approvalPerformed'])
        self.assertTrue(evidence_current(self.root, self.chapter)['fresh'])
        self.assertIsInstance(evidence_current(self.root, self.chapter)['passed'], bool)
        report = read_json(self.root / result['diagnostics']['artifactPath'])['payload']
        self.assertTrue(report['calibration']['advisory'])
        from scripts.core.repair_context import repair_context
        repair = repair_context(self.root, chapterId=self.chapter, limit=100)
        self.assertTrue(any(r['source'] == 'calibration' for r in repair['items']))
        self.assertTrue(any(r['source'] == 'paragraphCalibration' for r in repair['items']))

    def test_advance_failure_does_not_publish_candidate(self):
        source = load_structure(self.root); source['chapters'][0]['status'] = 'stable'
        write_json(self.root / '.govel/structure.json', source)
        before = (self.root / '.govel/workflow.json').read_bytes()
        (self.root / '工作台/input.txt').write_text('未登记正文。')
        with self.assertRaises(StructureError):
            pack_artifact(self.root, chapterId=self.chapter, artifactType='candidate_prose', input='工作台/input.txt', advanceLifecycle=True)
        self.assertEqual(before, (self.root / '.govel/workflow.json').read_bytes())


class RecordPatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); source_fixture(self.root)

    def test_fact_patch_reuses_approval_gate_and_preserves_other_fields(self):
        from scripts.core.structure_commands import commit_plan
        from scripts.core.source_content_proposals import resolve_content_target
        old = resolve_content_target(self.root, recordId='SF-LIFE', kind='fact')
        args = {'sessionRef': old['sessionRef'], 'expectedRecordHash': old['recordHash'],
                'patch': [{'op': 'text', 'path': ['text'], 'original': '守过', 'replacement': '走过'}],
                'reason': '核对事实', 'evidence': [{'fragmentId': 'SF-LIFE', 'quote': old['record']['text']}],
                'approvalActor': 'author', 'approvalEvidence': '确认'}
        plan = call_operation('source.content-proposal-preview', self.root, args)
        with self.assertRaises(StructureError):
            call_operation('source.content-proposal-preview', self.root, {**args, 'expectedRecordHash': '0'*64})
        with self.assertRaises(StructureError):
            commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=False)
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        new = resolve_content_target(self.root, recordId='SF-LIFE', kind='fact')['record']
        self.assertEqual({k: v for k, v in new.items() if k != 'text'}, {k: v for k, v in old['record'].items() if k != 'text'})
        self.assertIn('走过', new['text'])
        with self.assertRaises(StructureError):
            call_operation('source.content-proposal-preview', self.root, args)

    def test_patch_ambiguity_overlap_and_identity_changes_rejected(self):
        from scripts.core.record_patch import apply_record_patch
        from scripts.core.source_review import fingerprint
        record = {'text': '重复重复', 'nested': {'a': 1}}
        for edits in ([{'op': 'text', 'path': ['text'], 'original': '重复', 'replacement': '新'}],
                      [{'op': 'remove', 'path': ['nested']}, {'op': 'set', 'path': ['nested', 'a'], 'value': 2}]):
            with self.assertRaises(StructureError): apply_record_patch(record, edits, fingerprint(record))
        with self.assertRaises(StructureError):
            apply_record_patch({'text': 'aaa'}, [{'op': 'text', 'path': ['text'], 'original': 'aa', 'replacement': 'b'}], fingerprint({'text': 'aaa'}))
        resolved = call_operation('source.content-resolve', self.root, {'recordId': 'SF-LIFE', 'kind': 'fact'})
        with self.assertRaises(StructureError):
            call_operation('source.content-proposal-preview', self.root, {'sessionRef': resolved['sessionRef'],
                'expectedRecordHash': resolved['recordHash'], 'patch': [{'op': 'set', 'path': ['ownerId'], 'value': 'C-BETA'}],
                'reason': '测试', 'evidence': [], 'approvalActor': 'author', 'approvalEvidence': '确认'})

    def test_canon_patch_is_single_field_and_keeps_evidence_requirements(self):
        resolved = call_operation('source.canon-resolve', self.root, {'selector': 'C-ALPHA'})
        args = {'sessionRef': resolved['sessionRef'], 'expectedRecordHash': resolved['recordHash'],
                'patch': [{'op': 'set', 'path': ['summary'], 'value': '曾守城门'}], 'reason': '核对',
                'evidence': [{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}], 'approvalActor': 'author', 'approvalEvidence': '确认'}
        plan = call_operation('source.canon-proposal-preview', self.root, args)
        self.assertEqual(plan['parameters']['canonProposal']['newValue'], '曾守城门')
        with self.assertRaises(StructureError):
            call_operation('source.canon-proposal-preview', self.root, {**args, 'evidence': []})

    def test_attribute_nested_patch_and_planning_fragment_preserve_gates(self):
        from scripts.core.structure_commands import commit_plan
        old = call_operation('source.content-resolve', self.root, {'recordId': 'AT-HEIGHT', 'kind': 'attribute'})
        plan = call_operation('source.content-proposal-preview', self.root, {
            'sessionRef': old['sessionRef'], 'expectedRecordHash': old['recordHash'],
            'patch': [{'op': 'set', 'path': ['values', 0, 'value', 'number'], 'value': 185}],
            'reason': '核对', 'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '他身高一米九。'}],
            'approvalActor': 'author', 'approvalEvidence': '确认'})
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertEqual(load_structure(self.root)['attributes'][0]['values'][0]['value']['number'], 185)
        from tests.test_source_plan import planning_source
        planning_source(self.root)
        resolved = call_operation('source.content-resolve', self.root, {'recordId': 'SF-PLAN', 'kind': 'planning'})
        args = {'sessionRef': resolved['sessionRef'], 'expectedRecordHash': resolved['recordHash'],
                'patch': [{'op': 'text', 'path': ['text'], 'original': '守', 'replacement': '离开'}],
                'reason': '修订计划', 'evidence': [], 'approvalActor': 'author', 'approvalEvidence': '确认'}
        plan = call_operation('source.content-proposal-preview', self.root, args)
        self.assertEqual(plan['operation'], 'source_change')
        with self.assertRaises(StructureError):
            call_operation('source.content-proposal-preview', self.root, {**args,
                'patch': [{'op': 'set', 'path': ['contentRole'], 'value': 'fact'}]})

    def test_relation_patch_preserves_endpoints_and_author_declaration(self):
        source = load_structure(self.root)
        source['relations'] = [{'relationId': 'REL-ALPHA-BETA', 'fromEntity': 'C-ALPHA', 'toEntity': 'C-BETA', 'kind': 'protects'}]
        write_json(self.root / '.govel/structure.json', source)
        resolved = call_operation('source.relation-resolve', self.root, {'relationId': 'REL-ALPHA-BETA'})
        args = {'sessionRef': resolved['sessionRef'], 'expectedRecordHash': resolved['recordHash'],
                'patch': [{'op': 'set', 'path': ['kind'], 'value': 'trusts'}],
                'reason': '作者核对', 'evidence': [], 'evidenceMode': 'author_declaration',
                'approvalActor': 'author', 'approvalEvidence': '确认'}
        plan = call_operation('source.relation-proposal-preview', self.root, args)
        self.assertEqual(plan['parameters']['relationProposal']['newRecord']['kind'], 'trusts')
        with self.assertRaises(StructureError):
            call_operation('source.relation-proposal-preview', self.root, {**args,
                'patch': [{'op': 'set', 'path': ['fromEntity'], 'value': 'C-BETA'}]})


class TableAndCalibrationTests(unittest.TestCase):

    def test_calibration_offsets_are_exact_and_never_a_pass_decision(self):
        from scripts.core.prose_calibration import calibrate
        text = '甲开了门。乙关了窗。丙放下笔。\n\n显然，门开了。显然，门关了。'
        result = calibrate(text)
        self.assertFalse(result['affectsPassed'])
        self.assertEqual(len(result['paragraphs']), 2)
        hits = result['sepia']['template_hits']
        self.assertEqual(len(hits), 2)
        for hit in hits:
            self.assertEqual(text[hit['start']:hit['end']], hit['match'])
            self.assertEqual(hit['line'], 3)
        self.assertTrue(result['sepia']['flat_runs'])
        self.assertEqual(result['paragraphs'][0]['sentenceLengthVariance'], 0)

    def test_crlf_calibration_survives_diagnostics_and_repair_localization(self):
        from scripts.core.repair_context import repair_context
        from tests.fixtures_workflow import register_and_approve
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cid = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            text = '甲开了门。\r\n\r\n乙关了窗。\r\n\r\n显然，丙放下了笔。'
            (root / '工作台/crlf.txt').write_bytes(text.encode('utf-8'))
            result = pack_artifact(root, chapterId=cid, artifactType='candidate_prose',
                input='工作台/crlf.txt', advanceLifecycle=True, diagnostics={'mode': 'plot', 'intensityLevel': 1})
            self.assertTrue(result['ok'], result)
            payload = read_json(root / result['diagnostics']['artifactPath'])['payload']
            hit = payload['calibration']['sepia']['template_hits'][0]
            self.assertEqual(hit['start'], text.index('显然'))
            self.assertEqual(text[hit['start']:hit['end']], hit['match'])
            for index, paragraph in enumerate(payload['calibration']['paragraphs']):
                self.assertEqual(text[paragraph['start']:paragraph['end']].strip(),
                                 text.split('\r\n\r\n')[index])
            context = repair_context(root, chapterId=cid, limit=100)
            row = next(r for r in context['items'] if r.get('finding', {}).get('ruleId') == 'template_hits')
            self.assertEqual(row['location']['state'], 'unique')
            self.assertEqual(row['location']['spans'][0]['text'], '显然')
            self.assertEqual(row['location']['spans'][0]['startLine'], 5)
            hit['start'] -= 4; hit['end'] -= 4
            register_and_approve(root, cid, 'diagnostics', payload)
            context = repair_context(root, chapterId=cid, limit=100)
            row = next(r for r in context['items'] if r.get('finding', {}).get('ruleId') == 'template_hits')
            self.assertEqual(row['location']['spans'][0]['text'], '显然')
            self.assertEqual(row['location']['spans'][0]['start'], text.index('显然'))
