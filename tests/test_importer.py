from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core.importer import create_project_from_candidate, scan_import_source, validate_import_candidate
from scripts.core.structure_store import StructureError, empty_structure, load_structure, new_id


class ImporterTests(unittest.TestCase):
    def candidate(self):
        source = empty_structure('candidate-only', '外部原稿')
        candidate = {key: source[key] for key in ('bookName', 'volumes', 'arcs', 'chapters',
                     'chapterArcLinks', 'entities', 'relations', 'references',
                     'fieldDefinitions', 'attributes', 'fragments', 'units', 'presentations')}
        cid = new_id('chapter')
        candidate['chapters'].append({'chapterId': cid, 'displayOrdinal': 1, 'title': '开场',
            'placement': {'scope': 'volume', 'volumeId': source['volumes'][0]['volumeId']},
            'filePath': f'正文/{cid}.md', 'status': 'planning_required'})
        candidate['chapterArcLinks'].append({'chapterId': cid, 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
        return candidate

    def test_scan_only_creates_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "旧小说"
            source.mkdir()
            (source / "第001章_开场.md").write_text("# 第一章 开场\n正文", encoding="utf-8")
            result = scan_import_source(source)
            self.assertEqual(len(result["files"]), 1)
            self.assertEqual(result["files"][0]["chapterOrdinalHint"], 1)
            self.assertIn("requiredOutput", result["instructions"])

    def test_confirmed_candidate_creates_strict_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "new"
            volume_id, arc_id, chapter_id = new_id("volume"), new_id("arc"), new_id("chapter")
            candidate = {
                "bookName": "导入书",
                "sourceRoot": "/tmp/source",
                "volumes": [{"volumeId": volume_id, "ordinal": 1, "title": "第一卷", "status": "planned"}],
                "arcs": [{"arcId": arc_id, "title": "主线", "kind": "main", "status": "planned", "volumeIds": [volume_id], "parentArcId": None, "promise": "", "resolutionTargetChapterId": None}],
                "chapters": [{"chapterId": chapter_id, "displayOrdinal": 1, "displayCode": "CH001", "title": "开场", "placement": {"scope": "volume", "volumeId": volume_id}, "filePath": f"正文/{chapter_id}.md", "status": "planning_required", "storyTimeStartId": None, "storyTimeEndId": None}],
                "chapterArcLinks": [{"chapterId": chapter_id, "arcId": arc_id, "role": "primary", "weight": 1.0, "phaseId": None, "notes": ""}],
                "entities": [], "relations": [], "references": [],
            }
            original = deepcopy(candidate)
            create_project_from_candidate(root, candidate)
            self.assertEqual(candidate, original, 'import must not attach authority to the caller-owned candidate')
            structure = load_structure(root)
            self.assertEqual(structure["bookName"], "导入书")
            self.assertEqual(structure["chapters"][0]["chapterId"], chapter_id)
            self.assertEqual(structure['chapters'][0]['proseBinding']['state'], 'unbound')
            self.assertFalse((root/structure['chapters'][0]['filePath']).exists())
            from scripts.core.jsonio import read_json
            self.assertEqual(read_json(root/'.gnovel/workflow.json')['approvals'], {})

    def test_import_does_not_accept_project_snapshots_or_caller_authority(self):
        for field, value, code in (
            ('schemaVersion', 4, 'IMPORT_CANDIDATE_FIELDS_INVALID'),
            ('approvals', {'approved': True}, 'IMPORT_CANDIDATE_FIELDS_INVALID'),
            ('proseBinding', {'state': 'managed'}, 'OWNERSHIP_UNAUTHORIZED_MUTATION'),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                candidate = self.candidate()
                if field == 'proseBinding':
                    candidate['chapters'][0][field] = value
                else:
                    candidate[field] = value
                before = deepcopy(candidate)
                validation = validate_import_candidate(candidate)
                self.assertFalse(validation['valid'])
                self.assertIn(code, {row['code'] for row in validation['problems']})
                target = Path(temp)/'new'
                with self.assertRaises(StructureError):
                    create_project_from_candidate(target, candidate)
                self.assertFalse(target.exists())
                self.assertEqual(candidate, before)

    def test_import_rejects_old_chapter_layout_without_guessing_placement(self):
        candidate = self.candidate()
        chapter = candidate['chapters'][0]
        chapter['volumeId'] = chapter.pop('placement')['volumeId']
        before = deepcopy(candidate)
        validation = validate_import_candidate(candidate)
        self.assertFalse(validation['valid'])
        self.assertIn('CHAPTER_LEGACY_VOLUME_FORBIDDEN', {row['code'] for row in validation['problems']})
        self.assertEqual(candidate, before)


if __name__ == "__main__":
    unittest.main()
