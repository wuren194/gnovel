"""Current state previews verify source bindings, never manufacture Markdown changes."""
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import read_json, write_json
from scripts.core.setting_sync import state_preview
from scripts.core.structure_store import StructureError
from tests.fixtures_workflow import review_ready_project


class StatePreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def test_current_projection_is_checked_without_cache_or_markdown(self):
        for name in ('story-state.json', 'story-design.json'):
            (self.root/'.govel/cache'/name).unlink(missing_ok=True)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = state_preview(self.root, self.cid)
        self.assertTrue(result['baseMatches'])
        self.assertEqual(result['changes'], [])
        self.assertEqual(set(result['bindings']), {'baseStateSha256', 'designSha256', 'sourcePlanHash'})
        self.assertEqual(result['sourceChangesOperation'], 'source.change-preview')
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        self.assertFalse(list((self.root/'工作台').glob('*设定回写候选*')))

    def test_source_change_makes_binding_mismatch_visible(self):
        path = self.root/'.govel/structure.json'; source = read_json(path)
        source['revision'] += 1; write_json(path, source)
        self.assertFalse(state_preview(self.root, self.cid)['baseMatches'])

    def test_modified_or_invalidated_artifact_cannot_be_previewed_as_current(self):
        workflow_path = self.root/'.govel/workflow.json'; workflow = read_json(workflow_path)
        entry = workflow['artifacts'][self.cid]['state_candidate']
        artifact_path = Path(entry['artifactPath'])
        original = artifact_path.read_bytes(); artifact_path.write_bytes(original+b' ')
        with self.assertRaises(StructureError) as caught: state_preview(self.root, self.cid)
        self.assertEqual(caught.exception.code, 'ARTIFACT_FILE_CHANGED')
        artifact_path.write_bytes(original)
        entry['invalidatedBy'] = 'test-source-change'; write_json(workflow_path, workflow)
        with self.assertRaises(StructureError) as caught: state_preview(self.root, self.cid)
        self.assertEqual(caught.exception.code, 'STATE_CANDIDATE_MISSING')


if __name__ == '__main__': unittest.main()
