"""Chapter actions expose actual calls without granting new execution authority."""
from pathlib import Path
import tempfile
import unittest

from scripts.core.chapter_status import chapter_status
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import operations, validate_arguments
from tests.fixtures_workflow import review_ready_project


class ChapterActionBindingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def test_known_actions_use_real_schemas_and_unknown_inputs_remain_explicit(self):
        board = chapter_status(self.root, self.cid)
        specs = {spec.id: spec for spec in operations()}
        self.assertTrue(board['nextActions'])
        for row in board['nextActions']:
            self.assertIn('missingInputs', row)
            self.assertIn('executable', row)
            if row['operation'] is not None:
                self.assertIn(row['operation'], specs)
                if not row['missingInputs']:
                    validate_arguments(specs[row['operation']].input_schema, row['arguments'])
                self.assertEqual(row['arguments']['projectRoot'], str(self.root))
                self.assertNotIn('<', str(row['arguments']))
            if row['needsApproval'] or row.get('effects') != 'read':
                self.assertFalse(row['executable'])

    def test_approval_calls_reference_registered_files_but_never_execute(self):
        workflow = read_json(self.root / '.gnovel/workflow.json')
        workflow['approvals'] = {}
        write_json(self.root / '.gnovel/workflow.json', workflow)
        before = (self.root / '.gnovel/workflow.json').read_bytes()
        rows = chapter_status(self.root, self.cid)['nextActions']
        approval = [row for row in rows if row['operation'] == 'project.workflow.artifact-approve']
        self.assertTrue(approval)
        self.assertTrue(all(row['needsApproval'] and not row['executable'] for row in approval))
        self.assertTrue(any(row['arguments']['artifact'] for row in approval))
        self.assertEqual(before, (self.root / '.gnovel/workflow.json').read_bytes())

    def test_missing_diagnostics_reports_semantic_parameters_instead_of_placeholders(self):
        source = read_json(self.root/'.gnovel/structure.json')
        plan = next(f for f in source['fragments'] if f['ownerId'] == self.cid)
        plan['chapterContract'].pop('mode', None); plan['chapterContract'].pop('intensityLevel', None)
        write_json(self.root/'.gnovel/structure.json', source)
        from scripts.core.source_plan import chapter_plan
        from tests.fixtures_workflow import register_and_approve
        register_and_approve(self.root, self.cid, 'stage_a_review', {'decision': 'adopt', 'activeSeats': list(range(1, 11)),
            'sourcePlanHash': chapter_plan(source, self.cid)['sourcePlanHash'], 'consensus': [], 'disagreements': [], 'openItems': []})
        source = read_json(self.root / '.gnovel/structure.json')
        source['chapters'][0]['status'] = 'draft'
        write_json(self.root / '.gnovel/structure.json', source)
        workflow = read_json(self.root / '.gnovel/workflow.json')
        workflow['artifacts'][self.cid].pop('diagnostics')
        write_json(self.root / '.gnovel/workflow.json', workflow)
        row = chapter_status(self.root, self.cid)['nextActions'][0]
        self.assertEqual(row['operation'], 'diagnostics.aggregate')
        self.assertEqual(set(row['missingInputs']), {'mode', 'intensityLevel'})
        self.assertEqual(row['arguments']['chapterId'], self.cid)
        self.assertFalse(row['executable'])
