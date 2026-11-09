from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import novelctl, state_cache
from scripts.core.locks import project_lock, current_thread_owns_lock, project_lock_observation
from scripts.core.structure_commands import commit_plan, list_operations, preview_insert_chapter, undo_operation
from scripts.core.structure_store import StructureError
from tests.fixtures_current import create_schema5_project
from tests.fixtures_transaction import interrupted_write_set

ROOT = Path(__file__).resolve().parents[1]


class CliMutationLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root, chapter_count=1)
        self.chapter_id = self.fixture["chapterIds"][0]

    def args(self, *argv):
        return novelctl.build_parser().parse_args(["project", *argv, "--project-root", str(self.root)])

    def test_workflow_read_modify_write_is_inside_lock(self):
        original = novelctl._read_workflow
        observed = []

        def read_locked(root):
            observed.append(current_thread_owns_lock(root))
            return original(root)

        with patch.object(novelctl, "_read_workflow", side_effect=read_locked):
            novelctl._dispatch(self.args("workflow", "approve-chapters", "--chapter-id", self.chapter_id))
        self.assertEqual(observed, [True])
        self.assertFalse(project_lock_observation(self.root)["locked"])

    def test_explicit_cache_rebuild_writes_inside_lock(self):
        original = state_cache.rebuild
        observed = []
        def rebuild_locked(root):
            observed.append(current_thread_owns_lock(root))
            return original(root)
        with patch.object(state_cache, 'rebuild', side_effect=rebuild_locked):
            novelctl._dispatch(self.args('state', 'rebuild'))
        self.assertEqual(observed, [True])
        self.assertFalse(project_lock_observation(self.root)["locked"])


    def test_source_setting_check_is_read_only_and_lock_free(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with project_lock(self.root):
            result = novelctl._dispatch(self.args('settings', 'check'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})


    def test_concurrent_cli_mutation_is_rejected(self):
        with project_lock(self.root):
            with self.assertRaises(StructureError) as caught:
                novelctl._dispatch(self.args("workflow", "repair-round", "--chapter-id", self.chapter_id))
            self.assertEqual(caught.exception.code, "PROJECT_LOCKED")

    def test_all_unlocked_cli_writes_now_reject_pending_transaction(self):
        source = '.govel/structure.json'
        interrupted_write_set(self.root, {source: (self.root/source).read_bytes()})
        cases = [
            ["workflow", "approve-chapters", "--chapter-id", self.chapter_id],
            ["workflow", "repair-round", "--chapter-id", self.chapter_id],
            ["workflow", "artifact-register", "--chapter-id", self.chapter_id, "--artifact", "missing.json"],
            ["workflow", "artifact-approve", "--chapter-id", self.chapter_id, "--artifact-hash", "candidate_prose=invalid"],
            ["workflow", "set-next", "--chapter-id", self.chapter_id, "--next-action", "planner"],
            ["state", "rebuild"],
            ["workflow", "submit", "--request-id", "pending-submission", "--chapter-id", self.chapter_id,
             "--artifacts", '[{"artifactType":"title_candidates","payload":{"candidates":[{"title":"候选","reason":"测试"}]}}]'],
            ["summary", "commit", "--input", "missing.json"],
        ]
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for argv in cases:
            with self.subTest(argv=argv):
                proc = subprocess.run([sys.executable, str(ROOT / "scripts/novelctl.py"), "project", *argv, "--project-root", str(self.root)], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["code"], "TRANSACTION_INCOMPLETE")
                self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_cache_refresh_does_not_make_safe_undo_unavailable(self):
        from scripts.core.jsonio import read_json
        structure = self.fixture['structure']
        from scripts.core.catalog_commands import preview_update_volume
        volume_id = structure['volumes'][0]['volumeId']
        old_title = structure['volumes'][0]['title']
        plan = preview_update_volume(self.root, volume_id=volume_id, changes={'title': '改后的卷名'})
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        state_cache.rebuild(self.root)
        self.assertTrue(state_cache.is_fresh(self.root))
        operation = next(row for row in list_operations(self.root) if row['operationId'] == plan['operationId'])
        self.assertTrue(operation['undoable'], operation)
        before = read_json(self.root/'.govel/structure.json')
        result = undo_operation(self.root, plan['operationId'])
        self.assertEqual(result['code'], 'STRUCTURE_UNDONE')
        after = read_json(self.root/'.govel/structure.json')
        self.assertGreater(after['revision'], before['revision'])
        self.assertEqual(next(v for v in after['volumes'] if v['volumeId'] == volume_id)['title'], old_title)
        self.assertEqual([c['proseBinding'] for c in after['chapters']], [c['proseBinding'] for c in before['chapters']])
        state_cache.rebuild(self.root)
        self.assertTrue(state_cache.is_fresh(self.root))



if __name__ == "__main__":
    unittest.main()
