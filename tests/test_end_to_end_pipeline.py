from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.artifacts import envelope
from tests.cli_harness import cli_json

ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"


class EndToEndPipelineTests(unittest.TestCase):
    def run_cli(self, args: list[str]) -> tuple[int, dict]:
        if self._testMethodName != 'test_init_prompt_context_artifact_approval_loop':
            return cli_json(args)
        process = subprocess.run([sys.executable, str(NOVELCTL), *args], cwd=ROOT, capture_output=True, text=True)
        try:
            return process.returncode, json.loads(process.stdout)
        except json.JSONDecodeError:
            return process.returncode, {"stdout": process.stdout, "stderr": process.stderr}

    def insert_chapter(self, root: Path) -> tuple[str, dict]:
        code, graph = self.run_cli(["project", "structure", "show", "--project-root", str(root)])
        self.assertEqual(code, 0, graph)
        volume_id = graph["volumes"][0]["volumeId"]
        arc_id = graph["arcs"][0]["arcId"]
        code, preview = self.run_cli([
            "project", "structure", "insert-preview", "--project-root", str(root),
            "--title", "工作标题", "--volume-id", volume_id, "--primary-arc-id", arc_id,
        ])
        self.assertEqual(code, 0, preview)
        code, committed = self.run_cli([
            "project", "structure", "commit", "--project-root", str(root),
            "--operation-id", preview["operationId"], "--plan-hash", preview["planHash"], "--approve",
        ])
        self.assertEqual(code, 0, committed)
        cid = committed['chapter']['chapterId']
        code, source = self.run_cli(['project', 'structure', 'show', '--project-root', str(root)])
        changes = [
            {'action': 'put', 'collection': 'fieldDefinitions', 'record': {'fieldId': '$plan', 'key': 'plot.plan', 'label': '章节计划', 'valueType': 'topic', 'appliesTo': ['chapter']}},
            {'action': 'put', 'collection': 'attributes', 'record': {'attributeId': '$attribute', 'ownerId': cid, 'fieldId': '$plan', 'values': []}},
            {'action': 'put', 'collection': 'fragments', 'record': {'fragmentId': '$fragment', 'ownerId': cid, 'order': 1, 'contentRole': 'plan', 'contentBindings': ['$attribute'], 'text': '值班员交还钥匙。', 'chapterContract': {'targetRange': [1, 80], 'mode': 'plot', 'intensityLevel': 1}}},
        ]
        code, plan = self.run_cli(['project', 'source', 'change-preview', '--project-root', str(root), '--base-revision', str(source['revision']),
            '--request-id', 'e2e-plan', '--reason', '测试创作计划', '--changes', json.dumps(changes, ensure_ascii=False)])
        self.assertEqual(code, 0, plan)
        code, saved = self.run_cli(['project', 'source', 'commit', '--project-root', str(root), '--operation-id', plan['operationId'], '--plan-hash', plan['planHash'], '--approve'])
        self.assertEqual(code, 0, saved)
        return cid, committed['chapter']

    def test_init_prompt_context_artifact_approval_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            code, init = self.run_cli(["project", "quick-init", "--project-root", str(root), "--book-name", "端到端", "--no-agents-md"])
            self.assertEqual(code, 0, init)
            self.assertEqual(init["doctor"]["code"], "DOCTOR_OK")
            code, plan = self.run_cli(["project", "prompt-plan", "--project-root", str(root), "--station", "gnovel-writer"])
            self.assertEqual(code, 0, plan)
            self.assertTrue(plan["planHash"])
            chapter_id, _ = self.insert_chapter(root)
            artifact_path = root / "工作台" / "candidate.json"
            from scripts.core.source_plan import chapter_plan
            from scripts.core.structure_store import load_structure
            artifact = envelope("candidate_prose", {"text": "候选", 'sourcePlanHash': chapter_plan(load_structure(root), chapter_id)['sourcePlanHash']}, project_id=init["project"]["projectId"], chapter_id=chapter_id)
            artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
            code, registered = self.run_cli(["project", "workflow", "artifact-register", "--project-root", str(root), "--chapter-id", chapter_id, "--artifact", str(artifact_path)])
            self.assertEqual(code, 0, registered)
            code, approved = self.run_cli(["project", "workflow", "artifact-approve", "--project-root", str(root), "--chapter-id", chapter_id, "--artifact-hash", f"candidate_prose={artifact['artifactSha256']}"])
            self.assertEqual(code, 0, approved)
            self.assertEqual(approved["code"], "ARTIFACTS_APPROVED")

    def test_tampered_artifact_file_is_rejected_after_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            code, init = self.run_cli(["project", "quick-init", "--project-root", str(root), "--book-name", "篡改", "--no-agents-md"])
            self.assertEqual(code, 0, init)
            chapter_id, _ = self.insert_chapter(root)
            artifact_path = root / "工作台" / "candidate.json"
            from scripts.core.source_plan import chapter_plan
            from scripts.core.structure_store import load_structure
            artifact = envelope("candidate_prose", {"text": "候选", 'sourcePlanHash': chapter_plan(load_structure(root), chapter_id)['sourcePlanHash']}, project_id=init["project"]["projectId"], chapter_id=chapter_id)
            artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
            code, _ = self.run_cli(["project", "workflow", "artifact-register", "--project-root", str(root), "--chapter-id", chapter_id, "--artifact", str(artifact_path)])
            self.assertEqual(code, 0)
            artifact_path.write_text(json.dumps({**artifact, "payload": {"text": "被改"}}, ensure_ascii=False), encoding="utf-8")
            code, result = self.run_cli(["project", "workflow", "artifact-approve", "--project-root", str(root), "--chapter-id", chapter_id, "--artifact-hash", f"candidate_prose={artifact['artifactSha256']}"])
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "ARTIFACT_SOURCE_CHANGED")

    def test_unknown_chapter_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            code, init = self.run_cli(["project", "quick-init", "--project-root", str(root), "--book-name", "幽灵", "--no-agents-md"])
            self.assertEqual(code, 0, init)
            artifact_path = root / "工作台" / "candidate.json"
            artifact = envelope("candidate_prose", {"text": "候选"}, project_id=init["project"]["projectId"], chapter_id="CH-TEST")
            artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
            code, result = self.run_cli(["project", "workflow", "artifact-register", "--project-root", str(root), "--chapter-id", "CH-TEST", "--artifact", str(artifact_path)])
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "ARTIFACT_CHAPTER_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
