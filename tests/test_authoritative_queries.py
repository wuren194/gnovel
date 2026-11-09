"""Authoritative queries must not derive current facts from Markdown views."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.settings import query_settings
from scripts.core.structure_store import StructureError
from tests.fixtures_current import create_schema5_project
from tests.source_query_fixture import declare_field, set_field

ROOT = Path(__file__).resolve().parents[1]


class AuthoritativeQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root, chapter_count=1)
        self.structure = self.fixture['structure']
        self.entity = self.structure['entities'][0]
        set_field(self.structure, self.entity['entityId'], '声纹禁词', '当前值')
        set_field(self.structure, self.entity['entityId'], '备注', '完整的当前散文\n第二行')
        declare_field(self.structure, '空间布局', 'location')
        write_json(self.root / '.govel/structure.json', self.structure)

    def test_unrendered_entity_has_standard_and_custom_values(self):
        result = query_settings(self.root, selector=self.entity['entityId'])
        self.assertEqual(result['matchedCount'], 1)
        self.assertEqual(result['matches'][0]['fields']['声纹禁词'], '当前值')
        self.assertEqual(result['matches'][0]['fields']['备注'], '完整的当前散文\n第二行')
        self.assertEqual(result['matches'][0]['name'], self.entity['name'])
        self.assertEqual(query_settings(self.root, selector='阿砚')['matches'][0]['entityId'], self.entity['entityId'])

    def test_old_or_duplicate_cards_never_override_or_duplicate_truth(self):
        text = '| 字段 | 值 |\n|---|---|\n| 角色ID | ' + self.entity['entityId'] + ' |\n| 姓名 | 旧名字 |\n| 声纹禁词 | 旧值 |\n'
        (self.root / '设定/人物设定.md').write_text(text)
        split = self.root / '设定/人物' / (self.entity['entityId'] + '.md')
        split.parent.mkdir(); split.write_text(text)
        result = query_settings(self.root, selector=self.entity['entityId'])
        self.assertEqual(result['matchedCount'], 1)
        self.assertEqual(result['matches'][0]['fields']['声纹禁词'], '当前值')
        self.assertEqual(query_settings(self.root, selector='旧名字')['matchedCount'], 0)

    def test_missing_and_corrupt_structure_fail_closed(self):
        path = self.root / '.govel/structure.json'
        for contents in (None, '{broken'):
            with self.subTest(contents=contents):
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(contents)
                with self.assertRaises((StructureError, ValueError, OSError)):
                    query_settings(self.root)

    def test_counts_are_scoped_and_field_projection_is_not_filtering(self):
        result = query_settings(self.root, kind='location', fields=['空间布局'])
        self.assertEqual(result['matchedCount'], 1)
        self.assertEqual(set(result['kinds']), {'location'})
        self.assertEqual(result['matches'][0]['fields'], {})
        self.assertEqual(result['scope']['matchedEntities'], 1)

    def test_cli_reads_current_truth_without_writing(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'settings', 'show',
                                 self.entity['entityId'], '--field', '声纹禁词', '--project-root', str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['matches'][0]['fields'], {'声纹禁词': '当前值'})
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})


if __name__ == '__main__':
    unittest.main()
