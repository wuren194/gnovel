"""Atomic advancement and retry boundaries across legacy and single-source projects."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.jsonio import read_json, write_json
from scripts.core.mechanical_evidence import evidence_current
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_workflow import review_ready_project, register_and_approve


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        source = load_structure(self.root); source['chapters'][0]['status'] = 'planned'
        write_json(self.root / '.gnovel/structure.json', source)
        (self.root / '工作台/prose.txt').write_text('他推开门。')
        self.args = {'chapterId': self.cid, 'artifactType': 'candidate_prose', 'input': '工作台/prose.txt',
                     'advanceLifecycle': True, 'requestId': 'lifecycle-1', 'diagnostics': {'mode': 'plot', 'intensityLevel': 1}}

    def test_retry_does_not_reregister_or_rerun_or_regrant_approval(self):
        first = pack_artifact(self.root, **self.args)
        self.assertTrue(first['ok'], first)
        before = (self.root / '.gnovel/workflow.json').read_bytes()
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=AssertionError('must reuse')):
            second = pack_artifact(self.root, **self.args)
        self.assertTrue(second['replayed'])
        self.assertTrue(second['diagnostics']['reused'])
        self.assertFalse(second['approvalPerformed'])
        self.assertEqual(before, (self.root / '.gnovel/workflow.json').read_bytes())
        source = load_structure(self.root); source['entities'][0]['name'] += '改动'
        write_json(self.root / '.gnovel/structure.json', source)
        with self.assertRaises(StructureError): pack_artifact(self.root, **self.args)

    def test_replaying_superseded_pack_cannot_diagnose_the_new_candidate(self):
        first = pack_artifact(self.root, **self.args)
        self.assertTrue(first['ok'], first)
        (self.root / '工作台/other.txt').write_text('另一份待审稿。')
        pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', input='工作台/other.txt')
        before = (self.root / '.gnovel/workflow.json').read_bytes()
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=AssertionError('must not diagnose another candidate')):
            replay = pack_artifact(self.root, **self.args)
        self.assertFalse(replay['ok'])
        self.assertEqual(replay['diagnosticsError']['code'], 'DIAGNOSTICS_CANDIDATE_CHANGED')
        self.assertEqual(before, (self.root / '.gnovel/workflow.json').read_bytes())

    def test_diagnostic_failure_reports_committed_draft_and_retry_finishes(self):
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=OSError('test failure')):
            result = pack_artifact(self.root, **self.args)
        self.assertFalse(result['ok'])
        self.assertTrue(result['registered'])
        self.assertEqual(load_structure(self.root)['chapters'][0]['status'], 'draft')
        result = pack_artifact(self.root, **self.args)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['lifecycleStatus'], 'review')
        self.assertTrue(evidence_current(self.root, self.cid)['fresh'])

    def test_failed_atomic_candidate_publication_restores_structure_and_workflow(self):
        before = {p: (self.root / p).read_bytes() for p in ('.gnovel/structure.json', '.gnovel/workflow.json')}
        def fail(phase, relative=None):
            if phase == 'write' and relative == '.gnovel/structure.json':
                raise RuntimeError('injected write failure')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(RuntimeError): pack_artifact(self.root, **self.args)
        for p, raw in before.items(): self.assertEqual((self.root / p).read_bytes(), raw)
        self.assertFalse((self.root / '.gnovel/artifact-requests').exists() and list((self.root / '.gnovel/artifact-requests').glob('*.json')))

    def test_single_source_requires_current_stage_a_and_finishes_with_fresh_evidence(self):
        from tests.fixtures_current import initialize_project
        from scripts.core.structure_commands import preview_insert_chapter, commit_plan
        from scripts.core.source_changes import preview_source_change
        from scripts.core.source_plan import chapter_plan
        from scripts.core.chapter_state import advance_chapter_status
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = initialize_project(root)
            plan = preview_insert_chapter(root, title='守门', volume_id=source['volumes'][0]['volumeId'], primary_arc_id=source['arcs'][0]['arcId'])
            cid = commit_plan(root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)['chapter']['chapterId']
            source = load_structure(root)
            plan = preview_source_change(root, requestId='plan', baseRevision=source['revision'], reason='测试计划', changes=[
                {'action': 'put', 'collection': 'fieldDefinitions', 'record': {'fieldId': '$field', 'key': 'plot.plan', 'label': '计划', 'valueType': 'topic', 'appliesTo': ['chapter']}},
                {'action': 'put', 'collection': 'attributes', 'record': {'attributeId': '$attribute', 'ownerId': cid, 'fieldId': '$field', 'values': []}},
                {'action': 'put', 'collection': 'fragments', 'record': {'fragmentId': '$fragment', 'ownerId': cid, 'text': '甲守门。', 'order': 1, 'contentRole': 'plan', 'contentBindings': ['$attribute'],
                    'chapterContract': {'targetRange': [1, 1000],
                        'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}}},
            ])
            commit_plan(root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
            source = load_structure(root)
            register_and_approve(root, cid, 'stage_a_review', {'decision': 'adopt', 'activeSeats': list(range(1, 11)),
                'sourcePlanHash': chapter_plan(source, cid)['sourcePlanHash'], 'consensus': [], 'disagreements': [], 'openItems': []})
            advance_chapter_status(root, cid, 'planned')
            (root / '工作台/prose.txt').write_text('甲推开城门。')
            args = {**self.args, 'chapterId': cid}
            result = pack_artifact(root, **args)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['lifecycleStatus'], 'review')
            self.assertTrue(evidence_current(root, cid)['fresh'])
