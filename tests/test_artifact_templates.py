from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.core.artifact_templates import ARTIFACT_TYPES, artifact_template, validate_artifact_schema_declarations
from scripts.core.review_artifacts import validate_review_artifact

ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts/novelctl.py"


class ArtifactTemplateTests(unittest.TestCase):
    def test_all_templates_pass_runtime_validation(self) -> None:
        for artifact_type in ARTIFACT_TYPES:
            with self.subTest(artifact_type=artifact_type):
                validate_review_artifact(artifact_template(artifact_type, project_id="P", chapter_id="CH-X"))

    def test_schema_declarations_cover_runtime_types(self) -> None:
        report = validate_artifact_schema_declarations()
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(report["artifactTypes"]), set(ARTIFACT_TYPES))

    def test_cli_template_does_not_require_project(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(NOVELCTL), "project", "workflow", "artifact-template", "--type", "diagnostics", "--project-id", "P", "--chapter-id", "CH-X"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["artifactType"], "diagnostics")
        self.assertEqual(result["payload"]["passed"], False)


if __name__ == "__main__":
    unittest.main()
