"""Current source field selection, format rejection and read-only settings inspection."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.core.jsonio import read_json, write_json
from scripts.core.project import current_schema_version
from scripts.core.query import query_project
from scripts.core.settings import check_settings, query_settings
from scripts.core.structure_store import StructureError, WORKFLOW_VERSION
from tests.fixtures_source import source_fixture

ROOT = Path(__file__).resolve().parents[1]


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class SourceSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)
        write_json(self.root/'.govel/project.json', {
            'schemaVersion': current_schema_version(), 'workflowVersion': WORKFLOW_VERSION,
            'projectId': self.source['projectId'], 'bookName': self.source['bookName'], 'approvalMode': 'manual'})

    def test_validation_needs_only_current_source_and_never_creates_cards(self):
        before = inventory(self.root)
        report = check_settings(self.root)
        self.assertTrue(report['valid'])
        self.assertEqual(report['sources'], ['.govel/structure.json'])
        self.assertFalse(report['coverage']['semanticReviewComplete'])
        self.assertEqual(inventory(self.root), before)
        self.assertFalse((self.root/'设定').exists())

    def test_source_selectors_and_field_projection_are_stable(self):
        self.source['entities'][0]['aliases'] = ['守门人']
        write_json(self.root/'.govel/structure.json', self.source)
        for selector in ('character-ALPHA', '人物甲', '守门人'):
            with self.subTest(selector=selector):
                report = query_settings(self.root, selector=selector, fields=['肤色'])
                self.assertEqual(report['matchedCount'], 1)
                row = report['matches'][0]
                self.assertEqual(row['entityId'], 'character-ALPHA')
                self.assertEqual(row['valueSource'], '.govel/structure.json')
                self.assertEqual(row['fields']['appearance.skin'], '白皙\nsource-fragment:fact-LOOK')
                self.assertEqual([f['id'] for f in row['fragments']], ['fact-LOOK'])

    def test_field_ids_keys_and_labels_resolve_identically(self):
        rows = [query_settings(self.root, selector='character-ALPHA', fields=[field])['matches']
                for field in ('field-HEIGHT', 'appearance.height', '身高')]
        self.assertEqual(rows[0], rows[1]); self.assertEqual(rows[1], rows[2])
        value = rows[0][0]['fields']['appearance.height']
        self.assertIn('"number":190', value)
        self.assertIn('validDuring', value)
        self.assertIn('source-fragment:fact-HEIGHT', value)

    def test_exports_do_not_supply_missing_or_changed_field_truth(self):
        path = self.root/'设定/人物设定.md'; path.parent.mkdir()
        path.write_text('# 人物甲\n\n肤色：绿色。\n')
        before = inventory(self.root)
        result = query_settings(self.root, selector='character-ALPHA', fields=['肤色'])
        self.assertNotIn('绿色', json.dumps(result, ensure_ascii=False))
        self.assertEqual(inventory(self.root), before)
        source_path = self.root/'.govel/structure.json'; source_path.unlink()
        with self.assertRaises(StructureError): query_settings(self.root)
        self.assertEqual(path.read_text(), '# 人物甲\n\n肤色：绿色。\n')

    def test_other_project_formats_are_rejected_without_normalization(self):
        for version in (1, 2, 3, 4, 999):
            with self.subTest(version=version):
                source = deepcopy(self.source); source['schemaVersion'] = version
                write_json(self.root/'.govel/structure.json', source)
                before = inventory(self.root)
                for operation in (check_settings, query_settings):
                    with self.assertRaises(StructureError) as caught: operation(self.root)
                    self.assertEqual(caught.exception.code, 'UNSUPPORTED_PROJECT_VERSION')
                self.assertEqual(inventory(self.root), before)

    def test_missing_selector_and_unknown_fields_are_explicit(self):
        self.assertEqual(query_settings(self.root, selector='不存在')['matches'], [])
        with self.assertRaises(ValueError): query_settings(self.root, kind='不存在')
        with self.assertRaises(StructureError) as caught:
            query_settings(self.root, fields=['不存在'])
        self.assertEqual(caught.exception.code, 'SOURCE_FIELD_SELECTOR_INVALID')

    def test_four_dimension_queries_use_registered_definitions(self):
        result = query_project(self.root, 'attributes', selector='character-ALPHA', field=['肤色'])
        self.assertEqual(result['items'][0]['fields']['appearance.skin'], '白皙\nsource-fragment:fact-LOOK')
        schema = query_project(self.root, 'attributes', view='schema')
        fields = {row['field']: row['fieldKind'] for row in schema['items']}
        self.assertEqual(fields, {'appearance.height': 'measurement', 'appearance.skin': 'text', 'biography.experience': 'prose'})

    def test_cli_query_and_check_return_current_source_results(self):
        for args, code in ((['show', 'character-ALPHA', '--field', '肤色'], 'SETTINGS_QUERY'), (['check'], 'SETTINGS_CHECKED')):
            result = subprocess.run([sys.executable, str(ROOT/'scripts/novelctl.py'), 'project', 'settings', *args,
                '--project-root', str(self.root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload['code'], code)


if __name__ == '__main__': unittest.main()
