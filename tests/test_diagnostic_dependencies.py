"""Real runner outputs keep semantic credit across unrelated management changes."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.artifacts import sha256_json
from scripts.core.jsonio import read_json, write_json
from scripts.core.mechanical_evidence import evidence_current, run_diagnostics
from tests.fixtures_workflow import review_ready_project


class DiagnosticDependencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = review_ready_project(self.root, chapter_count=3)
        self.cid = self.fixture['chapterIds'][0]
        self.source_path = self.root / '.gnovel/structure.json'

    def test_unrelated_status_preserves_original_report_and_semantic_credit(self):
        workflow = read_json(self.root / '.gnovel/workflow.json')
        entry = workflow['artifacts'][self.cid]['diagnostics']
        path = Path(entry['artifactPath']); before = path.read_bytes()
        payload = read_json(path)['payload']
        self.assertEqual(payload['inputHash'], sha256_json({**payload['runInputs'], **payload['run']}))
        source = read_json(self.source_path)
        source['chapters'][1]['status'] = 'planned'; source['revision'] += 1
        write_json(self.source_path, source)
        self.assertTrue(evidence_current(self.root, self.cid)['fresh'])
        result = run_diagnostics(self.root, chapter_id=self.cid, mode='plot', intensity_level=1, register=True)
        self.assertEqual(result['code'], 'DIAGNOSTICS_REUSED')
        self.assertEqual(path.read_bytes(), before)
        from scripts.core.finalization_preflight import preflight_finalization
        self.assertFalse(preflight_finalization(self.root, self.cid, skip_title=True).ready,
                         'semantic reuse cannot authorize an old state/CAS')

    def test_actual_global_voice_consumer_change_requires_new_run(self):
        source = read_json(self.source_path)
        source['entities'][0]['voiceProfile']['bannedPhrasesForRole'].append('新禁词')
        write_json(self.source_path, source)
        self.assertFalse(evidence_current(self.root, self.cid)['fresh'])
        result = run_diagnostics(self.root, chapter_id=self.cid, mode='plot', intensity_level=1)
        self.assertEqual(result['code'], 'DIAGNOSTICS_EVIDENCE')
        self.assertEqual(result['evidence']['voiceCoverage']['characters'][0]['bannedPhraseCount'], 2)

    def test_agents_and_related_plan_are_consumed_inputs(self):
        self.assertTrue(evidence_current(self.root, self.cid)['fresh'])
        with (self.root / 'AGENTS.md').open('a') as stream:
            stream.write('\n第一卷 L1-L2\n')
        self.assertFalse(evidence_current(self.root, self.cid)['fresh'])
        source = read_json(self.source_path)
        source['fragments'][0]['text'] += '修改本章因果。'
        write_json(self.source_path, source)
        self.assertFalse(evidence_current(self.root, self.cid)['fresh'])

    def test_missing_or_modified_original_proof_is_not_refreshed(self):
        from scripts.core.diagnostic_inputs import stored_input_proof
        workflow = read_json(self.root / '.gnovel/workflow.json')
        original = read_json(Path(workflow['artifacts'][self.cid]['diagnostics']['artifactPath']))['payload']
        self.assertTrue(stored_input_proof(original))
        for field in ('runInputs', 'semanticInputs'):
            changed = deepcopy(original); changed.pop(field)
            self.assertFalse(stored_input_proof(changed))
        changed = deepcopy(original); changed['runInputs']['structureRevision'] += 1
        self.assertFalse(stored_input_proof(changed))
        changed = deepcopy(original); changed['semanticInputs']['draftSha256'] = '0' * 64
        self.assertFalse(stored_input_proof(changed))

    def test_reused_diagnostics_commit_draft_to_review_once_without_new_report(self):
        from unittest.mock import patch
        from scripts.core.chapter_status import chapter_status
        source = read_json(self.source_path)
        source['chapters'][0]['status'] = 'draft'
        write_json(self.source_path, source)
        workflow_path = self.root / '.gnovel/workflow.json'
        workflow = read_json(workflow_path)
        workflow['chapterStates'][self.cid] = {'status': 'draft'}
        write_json(workflow_path, workflow)
        original = deepcopy(workflow['artifacts'][self.cid]['diagnostics'])
        raw = Path(original['artifactPath']).read_bytes()
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=AssertionError('must reuse')):
            first = run_diagnostics(self.root, chapter_id=self.cid, register=True)
            self.assertTrue(first['reused'])
            result = run_diagnostics(self.root, chapter_id=self.cid, register=True, advance_lifecycle=True)
            self.assertTrue(result['reused'])
            self.assertEqual(chapter_status(self.root, self.cid)['stage'], 'review')
            self.assertEqual(read_json(workflow_path)['chapterStates'][self.cid]['status'], 'review')
            self.assertEqual(read_json(workflow_path)['artifacts'][self.cid]['diagnostics'], original)
            self.assertEqual(Path(original['artifactPath']).read_bytes(), raw)
            revision = read_json(self.source_path)['revision']
            run_diagnostics(self.root, chapter_id=self.cid, register=True, advance_lifecycle=True)
            self.assertEqual(read_json(self.source_path)['revision'], revision)
        self.assertTrue(evidence_current(self.root, self.cid)['fresh'])

    def test_reused_lifecycle_does_not_overwrite_concurrent_workflow_change(self):
        from unittest.mock import patch
        from scripts.core import mechanical_evidence
        source = read_json(self.source_path); source['chapters'][0]['status'] = 'draft'
        write_json(self.source_path, source)
        path = self.root / '.gnovel/workflow.json'
        workflow = read_json(path); workflow['chapterStates'][self.cid] = {'status': 'draft'}
        write_json(path, workflow)
        publish = mechanical_evidence._publish
        def concurrent(root, outputs, guards, original):
            current = read_json(path); current['authorNote'] = '并发保留的记录'
            write_json(path, current)
            return publish(root, outputs, guards, original)
        with patch.object(mechanical_evidence, '_publish', concurrent), self.assertRaises(ValueError) as caught:
            run_diagnostics(self.root, chapter_id=self.cid, register=True, advance_lifecycle=True)
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assertEqual(read_json(path)['authorNote'], '并发保留的记录')
        self.assertEqual(read_json(self.source_path)['chapters'][0]['status'], 'draft')
