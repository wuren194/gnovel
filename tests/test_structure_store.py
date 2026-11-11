from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.structure_store import StructureError, empty_structure, new_id, validate_structure


class StructureValidationTests(unittest.TestCase):
    def _valid(self) -> dict:
        data = empty_structure("project-1", "测试")
        chapter_id = new_id("chapter")
        data["chapters"] = [{
            "chapterId": chapter_id,
            "displayOrdinal": 1,
            "displayCode": "CH001",
            "title": "第一章",
            "placement": {"scope": "volume", "volumeId": data["volumes"][0]["volumeId"]},
            "filePath": f"正文/{chapter_id}.md",
            "status": "planned",
            "storyTimeStartId": None,
            "storyTimeEndId": None,
        }]
        data["chapterArcLinks"] = [{
            "chapterId": chapter_id,
            "arcId": data["arcs"][0]["arcId"],
            "role": "primary",
            "weight": 1.0,
            "phaseId": None,
            "notes": "",
        }]
        from scripts.core.prose_binding import seal_binding
        data['revision'] = 1
        data['chapters'][0]['proseBinding'] = seal_binding(data['projectId'], chapter_id,
            data['chapters'][0]['filePath'], state='unbound', generation=1, expected_content_digest=None)
        return data

    def test_valid_structure_passes(self) -> None:
        validate_structure(self._valid())

    def test_chapter_requires_exactly_one_primary_arc(self) -> None:
        data = self._valid()
        data["chapterArcLinks"] = []
        with self.assertRaises(StructureError) as caught:
            validate_structure(data)
        self.assertEqual(caught.exception.code, "STRUCTURE_INVALID")
        self.assertIn("PRIMARY_ARC_REQUIRED", {item["code"] for item in caught.exception.details["errors"]})

    def test_entity_names_and_aliases_are_global(self) -> None:
        data = self._valid()
        data["entities"] = [
            {"entityId": "character-ALPHA", "entityType": "character", "name": "沈砚", "aliases": ["阿砚"], "status": "active"},
            {"entityId": "prop-KEY", "entityType": "prop", "name": "钥匙", "aliases": ["阿砚"], "status": "active"},
        ]
        with self.assertRaises(StructureError) as caught:
            validate_structure(data)
        self.assertIn("ENTITY_NAME_COLLISION", {item["code"] for item in caught.exception.details["errors"]})

    def test_relations_must_resolve(self) -> None:
        data = self._valid()
        data["entities"] = [{"entityId": "character-ALPHA", "entityType": "character", "name": "沈砚", "aliases": [], "status": "active"}]
        data["relations"] = [{"relationId": "relation-ONE", "fromEntity": "character-ALPHA", "toEntity": "prop-MISSING", "relationType": "owns"}]
        with self.assertRaises(StructureError) as caught:
            validate_structure(data)
        self.assertIn("DANGLING_RELATION", {item["code"] for item in caught.exception.details["errors"]})


if __name__ == "__main__":
    unittest.main()
