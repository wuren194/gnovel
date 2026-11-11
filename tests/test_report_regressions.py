"""Current-format regressions from the November report, using neutral projects."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import write_json
from scripts.core.structure_store import load_structure
from tests.fixtures_current import create_current_project


class ReportRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture = create_current_project(self.root, chapter_count=3)

    def test_outline_constraints_read_the_supplied_new_and_changed_fragments(self):
        from scripts.core.outline_facts import inspect_outline
        cid = self.fixture['chapterIds'][0]
        source = load_structure(self.root)
        plan = next(row for row in source['fragments'] if row['ownerId'] == cid and row['contentRole'] == 'plan')
        text = 'AGENTS.md 偏离 L1：本轮需要明确的作者例外。'
        (self.root / 'AGENTS.md').write_text('中性测试项目。\n', encoding='utf-8')
        before = (self.root / '.govel/structure.json').read_bytes()
        for added in (False, True):
            proposed = deepcopy(source)
            changed = deepcopy(plan)
            changed['text'] = text
            if added:
                changed.update(fragmentId='plan-MEMORY-ONLY', order=2)
                changed.pop('chapterContract', None)
                proposed['fragments'].append(changed)
            else:
                proposed['fragments'][proposed['fragments'].index(plan)] = changed
            result = inspect_outline(self.root, chapterId=cid, structure=proposed)
            self.assertEqual(result['projectConstraints']['exceptionDeclaration'], text)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)

    def test_serial_policy_uses_placement_and_real_unfinished_chapters(self):
        from scripts.core.workflow import default_workflow, validate_next_action_for_project, WorkflowError
        source = load_structure(self.root)
        workflow = default_workflow(); workflow['approvedChapterIds'] = self.fixture['chapterIds']
        for row in source['chapters']:
            row['status'] = 'planning_required'
        volume = deepcopy(source['volumes'][0]); volume.update(volumeId='volume-NEXT', ordinal=2, status='planned')
        source['volumes'].append(volume)
        source['chapters'][2]['placement'] = {'scope': 'volume', 'volumeId': volume['volumeId']}
        action = lambda index, name='router': {'chapterId': self.fixture['chapterIds'][index], 'action': name}
        validate_next_action_for_project(action(0), source, workflow)
        with self.assertRaises(WorkflowError) as caught:
            validate_next_action_for_project(action(2), source, workflow)
        self.assertEqual(caught.exception.code, 'CHAPTER_OUTSIDE_CURRENT_VOLUME')
        validate_next_action_for_project(action(2), source, workflow, current_volume_only=False)
        with self.assertRaises(WorkflowError) as caught:
            validate_next_action_for_project(action(1, 'planner'), source, workflow)
        self.assertEqual(caught.exception.code, 'PREWARM_DISABLED')
        validate_next_action_for_project(action(1, 'planner'), source, workflow, prewarm_next_chapter=True)
        with self.assertRaises(WorkflowError) as caught:
            validate_next_action_for_project(action(2, 'planner'), source, workflow,
                                             current_volume_only=False, prewarm_next_chapter=True)
        self.assertEqual(caught.exception.code, 'PREWARM_DISABLED')
        source['chapters'][1]['status'] = 'planned'
        with self.assertRaises(WorkflowError) as caught:
            validate_next_action_for_project(action(1, 'writer'), source, workflow, prewarm_next_chapter=True)
        self.assertEqual(caught.exception.code, 'PREDECESSOR_UNSTABLE')
        source['chapters'][0]['status'] = source['chapters'][1]['status'] = 'stable'
        validate_next_action_for_project(action(2), source, workflow)
