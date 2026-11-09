"""The current-format boundary rejects old projects without interpreting or mutating them."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import write_json
from scripts.core.operations import call_operation, operations
from scripts.core.project import current_schema_version
from scripts.core.source_impact import dependency_edges, source_impact
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_current import initialize_project
from tests.fixtures_source import source_fixture, single_source_chapter


class CurrentFormatOnlyTests(unittest.TestCase):
    def test_old_formats_cannot_be_read_or_written_through_public_project_operations(self):
        for version in (1, 2, 3, 4, True, '5'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = initialize_project(root)
                source['schemaVersion'] = version
                write_json(root/'.govel/structure.json', source)
                before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                for operation, args in (
                        ('source.query', {}),
                        ('workflow.submit', {'requestId': 'cannot-resume'}),
                        ('source.change-preview', {'requestId': 'cannot-write', 'baseRevision': 0,
                            'reason': 'test rejection', 'changes': [{'action': 'put', 'collection': 'entities',
                                'record': {'entityId': '$person', 'entityType': 'character', 'name': '合成人物'}}]})):
                    with self.assertRaises(StructureError) as caught:
                        call_operation(operation, root, args)
                    self.assertEqual(caught.exception.code, 'UNSUPPORTED_PROJECT_VERSION')
                self.assertEqual(before, {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_new_project_has_one_layout_without_format_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_project(root)
            self.assertEqual(load_structure(root)['schemaVersion'], current_schema_version())
            self.assertFalse(list(root.rglob('*.md')))
        specs = {spec.id: spec for spec in operations()}
        self.assertNotIn('singleSource', specs['project.quick-init'].input_schema['properties'])
        self.assertFalse([key for key in specs if key.startswith(('source.migration-', 'source.schema-upgrade-',
            'project.ownership-migration.', 'project.migration.', 'project.upgrade.'))])

    def test_current_time_mounts_participate_in_source_impact(self):
        source = source_fixture()
        chapter = single_source_chapter(source, 'CH-TIME-DEPENDENT', volume_id=source['volumes'][0]['volumeId'])
        chapter['storyTimeStartId'] = 'TA-START'
        source['chapters'].append(chapter)
        source['chapterArcLinks'].append({'chapterId': chapter['chapterId'], 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
        source['entities'][0]['timeStartId'] = 'TA-START'
        self.assertIn(('TA-START', chapter['chapterId'], 'time_mount'), dependency_edges(source))
        self.assertIn(('TA-START', 'C-ALPHA', 'time_mount'), dependency_edges(source))
        after = deepcopy(source)
        next(row for row in after['entities'] if row['entityId'] == 'TA-START')['timeOrdinal'] = 3
        impact = source_impact(source, after)
        self.assertIn(chapter['chapterId'], impact['staleCandidates'])

    def test_old_chapter_outline_locator_is_rejected_without_conversion(self):
        from scripts.core.structure_store import validate_structure
        source = source_fixture()
        chapter = single_source_chapter(source, 'CH-OUTLINE-REJECT', volume_id=source['volumes'][0]['volumeId'])
        chapter['outlinePath'] = '规划/旧章纲.md'
        source['chapters'].append(chapter)
        source['chapterArcLinks'].append({'chapterId': chapter['chapterId'], 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
        with self.assertRaises(StructureError) as caught:
            validate_structure(source)
        self.assertIn('CHAPTER_OUTLINE_LOCATOR_FORBIDDEN', {row['code'] for row in caught.exception.details['errors']})


if __name__ == '__main__':
    unittest.main()
