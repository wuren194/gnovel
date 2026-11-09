"""Explicit reading snapshots never become a maintained second source."""
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import read_json
from scripts.core.markdown_sync import write_view_file
from scripts.core.paths import ProjectPathError
from scripts.core.source_render import render_source
from scripts.core.structure_commands import commit_plan, preview_insert_chapter
from scripts.core.structure_store import StructureError
from tests.fixtures_current import initialize_project
from tests.fixtures_source import source_fixture


class ReadingSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = initialize_project(self.root)

    def test_graph_export_is_explicit_and_idempotent_without_view_ledger(self):
        result = write_view_file(self.root, '工作台/关系图.md', title='关系', mermaid='graph LR\n  A --> B')
        self.assertTrue(result['written']); self.assertFalse(result['maintained'])
        path = Path(result['path']); before = path.read_bytes()
        result = write_view_file(self.root, '工作台/关系图.md', title='关系', mermaid='graph LR\n  A --> B')
        self.assertFalse(result['written']); self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root/'.gnovel/views.json').exists())

    def test_protected_destinations_and_traversal_are_rejected(self):
        for relative in ('正文/覆盖.md', '设定/覆盖.md', '规划/覆盖.md', '.gnovel/覆盖.md', '../覆盖.md'):
            with self.subTest(relative=relative), self.assertRaises((StructureError, ProjectPathError)):
                write_view_file(self.root, relative, title='关系', mermaid='graph LR')
            self.assertFalse((self.root/relative).exists())

    def test_existing_authored_or_changed_snapshot_is_never_overwritten(self):
        path = self.root/'工作台/关系图.md'; path.write_text('作者内容')
        with self.assertRaises(StructureError) as caught:
            write_view_file(self.root, '工作台/关系图.md', title='关系', mermaid='graph LR')
        self.assertEqual(caught.exception.code, 'EXPORT_EXISTS')
        self.assertEqual(path.read_text(), '作者内容')

    def test_symlink_destination_is_rejected(self):
        outside = self.root.parent/(self.root.name+'-outside.md'); outside.write_text('外部原文')
        self.addCleanup(outside.unlink)
        path = self.root/'工作台/链接.md'; path.symlink_to(outside)
        with self.assertRaises(ProjectPathError):
            write_view_file(self.root, '工作台/链接.md', title='关系', mermaid='graph LR')
        self.assertEqual(outside.read_text(), '外部原文')

    def test_structure_commit_ignores_human_markdown_and_produces_no_indexes(self):
        authored = self.root/'设定/实体索引.md'; authored.write_text('作者自己整理的阅读笔记')
        before = authored.read_bytes()
        plan = preview_insert_chapter(self.root, title='交班', volume_id=self.source['volumes'][0]['volumeId'],
                                      primary_arc_id=self.source['arcs'][0]['arcId'])
        targets = {row['path'] for row in plan['writeSet']}
        self.assertFalse(any(path.startswith(('设定/', '规划/')) for path in targets))
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertEqual(authored.read_bytes(), before)
        self.assertFalse((self.root/'规划/结构索引.md').exists())
        self.assertFalse((self.root/'.gnovel/views.json').exists())

    def test_source_render_is_read_only_and_retains_selected_text(self):
        source_fixture(self.root)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = render_source(self.root)
        self.assertIn('他独自守过城门。', result['markdown'])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)


if __name__ == '__main__': unittest.main()
