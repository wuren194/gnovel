"""Registration-time binding fills and file-based approval keep the trust boundary.

三种 hash 分离：绑定内容 hash（按字段语义）、工件信封 hash（注入后重算）、批准 hash
（仍对照注册条目）。显式 hash 批准和完整工件注册同样验证当前绑定。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import state_cache
from scripts.core.artifact_bindings import ArtifactBindError, bind_fields
from scripts.core.artifacts import envelope, file_sha256, sha256_text
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_plan import chapter_plan
from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS, validate_review_artifact
from scripts.core.workflow import approve_artifacts, record_artifact
from tests.fixtures_workflow import register_and_approve, review_ready_project, evidenced_slots

ROOT = Path(__file__).resolve().parents[1]


def cli(*argv: str) -> dict:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/novelctl.py"), *argv],
                          capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"CLI produced no JSON: {proc.stderr}\nstdout: {proc.stdout[:400]}")
    payload["__returncode"] = proc.returncode
    return payload


class BindingFillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = review_ready_project(self.root, chapter_count=1)
        self.chapter = self.fixture["chapterIds"][0]

    def _workflow(self) -> dict:
        return read_json(self.root / ".govel/workflow.json")

    def test_stage_b_binds_prose_from_registered_candidate(self):
        """裸字段绑定：从已注册 candidate_prose 计算正文 hash，注入后信封自洽、可入库。"""
        workflow = self._workflow()
        structure = read_json(self.root / ".govel/structure.json")
        artifact = envelope("stage_b_review", {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'},
            "verdict": "POLISH_READY",
            "slots": evidenced_slots('他把钥匙放回桌上，合上了登记本。'),
            "findings": [],
        }, project_id=structure["projectId"], chapter_id=self.chapter)
        filled, provenance = self._bind(artifact, [("finalProseSha256", None), ("sourcePlanHash", None)])
        self.assertEqual(provenance["finalProseSha256"]["source"], "current_truth")
        validate_review_artifact(filled)
        registered = json.loads((self.root / ".govel/workflow.json").read_text())  # unchanged by bind
        prose_payload = read_json(Path(registered["artifacts"][self.chapter]["candidate_prose"]["artifactPath"]))
        self.assertEqual(filled["payload"]["finalProseSha256"], sha256_text(prose_payload["payload"]["text"]))
        record_artifact(workflow, self.chapter, filled, structure=structure,
                        project_id=structure["projectId"], project_root=self.root)
        self.assertEqual(
            workflow["artifacts"][self.chapter]["stage_b_review"]["artifactSha256"],
            filled["artifactSha256"])

    def _bind(self, artifact: dict, bindings):
        from scripts.core.artifact_bindings import bind_fields
        return bind_fields(self.root, structure=read_json(self.root / ".govel/structure.json"),
                           workflow=self._workflow(), chapter_id=self.chapter,
                           artifact=artifact, bindings=bindings)

    def test_bind_file_mismatch_is_rejected_before_registration(self):
        """带路径绑定必须与当前真源一致：绑定漂移的文件在注册时就被拒绝。"""
        stranger = self.root / "工作台" / "外稿.md"
        stranger.write_text("别的稿子", encoding="utf-8")
        structure = read_json(self.root / ".govel/structure.json")
        artifact = envelope("stage_b_review", {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'},
            "verdict": "POLISH_READY",
            "slots": evidenced_slots('他把钥匙放回桌上，合上了登记本。'),
            "findings": [],
        }, project_id=structure["projectId"], chapter_id=self.chapter)
        from scripts.core.artifact_bindings import ArtifactBindError, bind_fields
        with self.assertRaises(ArtifactBindError) as caught:
            bind_fields(self.root, structure=structure, workflow=self._workflow(),
                        chapter_id=self.chapter, artifact=artifact,
                        bindings=[("finalProseSha256", str(stranger))])
        self.assertEqual(caught.exception.code, "BIND_PROSE_MISMATCH")

    def test_bind_file_match_accepts_explicit_source(self):
        registered = self._workflow()["artifacts"][self.chapter]["candidate_prose"]
        prose_text = json.loads(Path(registered["artifactPath"]).read_bytes())["payload"]["text"]
        prose_path = self.root / "工作台" / "正文副本.md"
        prose_path.write_text(prose_text, encoding="utf-8")
        structure = read_json(self.root / ".govel/structure.json")
        artifact = envelope("diagnostics", {"passed": True},
                            project_id=structure["projectId"], chapter_id=self.chapter)
        from scripts.core.artifact_bindings import bind_fields
        filled, provenance = bind_fields(self.root, structure=structure, workflow=self._workflow(),
                                         chapter_id=self.chapter, artifact=artifact,
                                         bindings=[("draftSha256", str(prose_path))])
        self.assertEqual(filled["payload"]["draftSha256"], sha256_text(prose_text))
        self.assertEqual(provenance["draftSha256"]["sourcePath"], str(prose_path.resolve()))

    def test_unsupported_and_duplicate_fields_are_rejected(self):
        structure = read_json(self.root / ".govel/structure.json")
        from scripts.core.artifact_bindings import ArtifactBindError, bind_fields
        diagnostics = envelope("diagnostics", {"passed": True},
                               project_id=structure["projectId"], chapter_id=self.chapter)
        with self.assertRaises(ArtifactBindError) as caught:
            bind_fields(self.root, structure=structure, workflow=self._workflow(),
                        chapter_id=self.chapter, artifact=diagnostics,
                        bindings=[("finalProseSha256", None)])
        self.assertEqual(caught.exception.code, "BIND_FIELD_UNSUPPORTED")
        stage_b = envelope("stage_b_review", {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'},
            "verdict": "POLISH_READY",
            "slots": evidenced_slots('他把钥匙放回桌上，合上了登记本。'),
            "findings": [],
        }, project_id=structure["projectId"], chapter_id=self.chapter)
        with self.assertRaises(ArtifactBindError) as caught:
            bind_fields(self.root, structure=structure, workflow=self._workflow(),
                        chapter_id=self.chapter, artifact=stage_b,
                        bindings=[("finalProseSha256", None), ("finalProseSha256", None)])
        self.assertEqual(caught.exception.code, "BIND_FIELD_DUPLICATE")

    def test_state_binding_uses_current_source_and_ignores_cache(self):
        state, design = state_cache.build_state(self.root), state_cache.build_design(self.root)
        structure = read_json(self.root / '.govel/structure.json')
        artifact = envelope('state_candidate', {'finalProseSha256': sha256_text('他把钥匙放回桌上，合上了登记本。'),
            'changes': []}, project_id=structure['projectId'], chapter_id=self.chapter)
        from scripts.core.artifacts import sha256_json
        for name in ('state', 'design'):
            (self.root/f'.govel/cache/story-{name}.json').write_text('damaged optional cache')
        filled, _ = bind_fields(self.root, structure=structure, workflow=self._workflow(),
            chapter_id=self.chapter, artifact=artifact, bindings=[('baseStateSha256', None), ('designSha256', None)])
        self.assertEqual(filled['payload']['baseStateSha256'], sha256_json(state))
        self.assertEqual(filled['payload']['designSha256'], sha256_json(design))

    def test_plan_binding_is_source_hash_and_rejects_markdown_binding(self):
        from scripts.core.source_plan import chapter_plan
        from scripts.core.artifacts import sha256_json
        structure = read_json(self.root/'.govel/structure.json')
        artifact = envelope('stage_a_review', {'decision': 'adopt', 'activeSeats': [1]},
                            project_id=structure['projectId'], chapter_id=self.chapter)
        with self.assertRaises(ArtifactBindError) as caught:
            bind_fields(self.root, structure=structure, workflow=self._workflow(), chapter_id=self.chapter,
                        artifact=artifact, bindings=[('outlineSha256', None)])
        self.assertEqual(caught.exception.code, 'BIND_FIELD_UNSUPPORTED')
        filled, _ = bind_fields(self.root, structure=structure, workflow=self._workflow(), chapter_id=self.chapter,
                               artifact=artifact, bindings=[('sourcePlanHash', None)])
        self.assertEqual(filled['payload']['sourcePlanHash'], chapter_plan(structure, self.chapter)['sourcePlanHash'])
        self.assertEqual(filled['artifactSha256'], sha256_json({k: v for k, v in filled.items() if k != 'artifactSha256'}))


class ApprovalByFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = review_ready_project(self.root, chapter_count=2)
        self.chapter = self.fixture["chapterIds"][0]

    def test_approve_by_file_matches_registered_hash(self):
        """--artifact @file：从文件读信封 hash，批准当前登记版本。"""
        workflow = read_json(self.root / ".govel/workflow.json")
        entry = workflow["artifacts"][self.chapter]["candidate_prose"]
        result = cli("project", "workflow", "artifact-approve", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--artifact", "@" + entry["artifactPath"])
        self.assertTrue(result["ok"], result)
        workflow = read_json(self.root / ".govel/workflow.json")
        self.assertEqual(workflow["approvals"][self.chapter]["artifactHashes"]["candidate_prose"],
                         entry["artifactSha256"])

    def test_approve_by_stale_file_is_rejected(self):
        """文件内容与注册条目不一致时批准失败（不能借路径绕过注册约束）。"""
        workflow = read_json(self.root / ".govel/workflow.json")
        path = Path(workflow["artifacts"][self.chapter]["candidate_prose"]["artifactPath"])
        forged = read_json(path)
        forged["payload"]["text"] = "篡改后的正文"
        forged["artifactSha256"] = "0" * 64
        write_json(path, forged)
        result = cli("project", "workflow", "artifact-approve", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--artifact", "@" + str(path))
        self.assertFalse(result["ok"])
        self.assertIn(result.get("code", ""), {"ARTIFACT_APPROVAL_HASH_MISMATCH", "ARTIFACT_HASH_MISMATCH"})

    def test_approve_hash_and_file_conflict_is_rejected(self):
        workflow = read_json(self.root / ".govel/workflow.json")
        entry = workflow["artifacts"][self.chapter]["candidate_prose"]
        result = cli("project", "workflow", "artifact-approve", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--artifact", "@" + entry["artifactPath"],
                     "--artifact-hash", f"candidate_prose={'a' * 64}")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ARTIFACT_HASH_CONFLICT")

    def test_explicit_hash_approval_matches_registered_artifact(self):
        """显式 hash 批准仍绑定当前已登记工件。"""
        workflow = read_json(self.root / ".govel/workflow.json")
        entry = workflow["artifacts"][self.chapter]["candidate_prose"]
        result = cli("project", "workflow", "artifact-approve", "--project-root", str(self.root),
                     "--chapter-id", self.chapter,
                     "--artifact-hash", f"candidate_prose={entry['artifactSha256']}")
        self.assertTrue(result["ok"], result)

    def test_approve_requires_at_least_one_source(self):
        result = cli("project", "workflow", "artifact-approve", "--project-root", str(self.root),
                     "--chapter-id", self.chapter)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ARTIFACT_HASH_INVALID")

    def test_chapter_mismatch_is_rejected(self):
        """信封 chapterId 与目标章不同时拒绝，防止跨章误批准。"""
        other = self.fixture["chapterIds"][-1]
        self.assertNotEqual(other, self.chapter)
        workflow = read_json(self.root / ".govel/workflow.json")
        entry = workflow["artifacts"][self.chapter]["candidate_prose"]
        result = cli("project", "workflow", "artifact-approve", "--project-root", str(self.root),
                     "--chapter-id", other, "--artifact", "@" + entry["artifactPath"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ARTIFACT_CHAPTER_MISMATCH")


class RegisterBindCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = review_ready_project(self.root, chapter_count=1)
        self.chapter = self.fixture["chapterIds"][0]

    def test_cli_register_with_bind_and_approve_by_file(self):
        """端到端：写一个缺 hash 的 stage_b，注册时 --bind 注入，批准按文件，全程零手抄 hash。"""
        structure = read_json(self.root / ".govel/structure.json")
        artifact = envelope("stage_b_review", {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'},
            "verdict": "POLISH_READY",
            "slots": evidenced_slots('他把钥匙放回桌上，合上了登记本。'),
            "findings": [],
        }, project_id=structure["projectId"], chapter_id=self.chapter)
        path = self.root / "工作台" / f"{self.chapter}-stage_b_review.json"
        write_json(path, artifact)
        result = cli("project", "workflow", "artifact-register", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--artifact", str(path),
                     "--bind", "finalProseSha256", "--bind", "sourcePlanHash")
        self.assertTrue(result["ok"], result)
        self.assertIn("finalProseSha256", result["bindings"])
        registered = read_json(path)
        self.assertTrue(registered["payload"]["finalProseSha256"])
        approve = cli("project", "workflow", "artifact-approve", "--project-root", str(self.root),
                      "--chapter-id", self.chapter, "--artifact", "@" + str(path))
        self.assertTrue(approve["ok"], approve)
        workflow = read_json(self.root / ".govel/workflow.json")
        self.assertEqual(workflow["approvals"][self.chapter]["artifactHashes"]["stage_b_review"],
                         registered["artifactSha256"])
        self.assertEqual(workflow["artifacts"][self.chapter]["stage_b_review"]["artifactSha256"],
                         registered["artifactSha256"])
        # 工件条目上的 provenance 记录绑定的来源与内容 hash，审计不再依赖会话记忆
        self.assertEqual(workflow["artifacts"][self.chapter]["stage_b_review"]["bindings"]["finalProseSha256"]["contentSha256"],
                         registered["payload"]["finalProseSha256"])

    def test_bind_registration_failures_preserve_both_files(self):
        from unittest.mock import patch
        from scripts.novelctl import _dispatch, build_parser, NovelError
        structure = read_json(self.root / '.govel/structure.json')
        artifact = envelope('diagnostics', {'passed': True},
                            project_id=structure['projectId'], chapter_id=self.chapter)
        workflow_path = self.root / '.govel/workflow.json'
        for relative in ('forbidden.json', '工作台/bound.json'):
            path = self.root / relative
            write_json(path, artifact)
            before = path.read_bytes(), workflow_path.read_bytes()
            args = build_parser().parse_args([
                'project', 'workflow', 'artifact-register', '--project-root', str(self.root),
                '--chapter-id', self.chapter, '--artifact', str(path), '--bind', 'draftSha256'])
            if relative == 'forbidden.json':
                with self.assertRaises(NovelError):
                    _dispatch(args)
            else:
                for fail_at in ('.govel/workflow.json', relative):
                    def fail(phase, target=None):
                        if phase == 'write' and target == fail_at:
                            raise OSError('injected registration write failure')
                    with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
                        with self.assertRaises(OSError):
                            _dispatch(args)
                    self.assertEqual((path.read_bytes(), workflow_path.read_bytes()), before)
            self.assertEqual((path.read_bytes(), workflow_path.read_bytes()), before)

    def test_cli_register_complete_payload_preserves_bytes(self):
        """完整载荷注册不触碰 payload，不伪造字段注入来源。"""
        structure = read_json(self.root / ".govel/structure.json")
        prose = "他把钥匙放回桌上，合上了登记本。"
        artifact = envelope("stage_b_review", {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'},
            "verdict": "POLISH_READY", "finalProseSha256": sha256_text(prose),
            "sourcePlanHash": chapter_plan(structure, self.chapter)["sourcePlanHash"],
            "slots": evidenced_slots('他把钥匙放回桌上，合上了登记本。'),
            "findings": [],
        }, project_id=structure["projectId"], chapter_id=self.chapter)
        path = self.root / "工作台" / f"{self.chapter}-complete.json"
        write_json(path, artifact)
        before = path.read_bytes()
        result = cli("project", "workflow", "artifact-register", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--artifact", str(path))
        self.assertTrue(result["ok"], result)
        self.assertNotIn("bindings", result)
        self.assertEqual(path.read_bytes(), before)
        workflow = read_json(self.root / ".govel/workflow.json")
        self.assertNotIn("bindings", workflow["artifacts"][self.chapter]["stage_b_review"])

    def test_cli_register_bind_rejects_unknown_field(self):
        structure = read_json(self.root / ".govel/structure.json")
        artifact = envelope("diagnostics", {"passed": True},
                            project_id=structure["projectId"], chapter_id=self.chapter)
        path = self.root / "工作台" / f"{self.chapter}-diag.json"
        write_json(path, artifact)
        result = cli("project", "workflow", "artifact-register", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--artifact", str(path),
                     "--bind", "outlineSha256")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "BIND_FIELD_UNSUPPORTED")


class TrustBoundaryTests(unittest.TestCase):
    """批准与校验的信任边界不因新入口改变。"""

    def test_approve_artifacts_still_requires_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture = review_ready_project(root, chapter_count=1)
            chapter = self.fixture["chapterIds"][0]
            workflow = read_json(root / ".govel/workflow.json")
            structure = read_json(root / ".govel/structure.json")
            outsider = envelope("title_candidates", {"candidates": [{"candidateId": "T", "title": "x"}]},
                                project_id=structure["projectId"], chapter_id=chapter)
            with self.assertRaises(Exception):
                approve_artifacts(workflow, chapter, artifact_hashes={"title_candidates": outsider["artifactSha256"]},
                                  approval_mode="manual")

    def test_finalize_still_blocks_tampered_registered_artifact(self):
        """bind_fields 只填字段不做语义结论：注册后篡改工件文件仍被 finalization 拦下。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = review_ready_project(root, chapter_count=1)
            chapter = fixture["chapterIds"][0]
            from scripts.core.finalization_preflight import preflight_finalization
            workflow = read_json(root / ".govel/workflow.json")
            entry = workflow["artifacts"][chapter]["stage_b_review"]
            data = read_json(Path(entry["artifactPath"]))
            data["payload"]["finalProseSha256"] = "f" * 64  # 与已注册 candidate_prose 不一致
            from scripts.core.artifacts import sha256_json
            data["artifactSha256"] = sha256_json({k: v for k, v in data.items() if k != "artifactSha256"})
            write_json(Path(entry["artifactPath"]), data)
            report = preflight_finalization(root, chapter)
            self.assertFalse(report.ready)
            # 磁盘文件与注册指纹不一致（ARTIFACT_SOURCE_CHANGED），信封 hash 与注册条目
            # 不一致（ARTIFACT_APPROVAL_HASH_MISMATCH），缺标题决定照旧拦下
            codes = {b["code"] for b in report.blockers}
            self.assertIn("ARTIFACT_SOURCE_CHANGED", codes)
            self.assertIn("TITLE_DECISION_REQUIRED", codes)


if __name__ == "__main__":
    unittest.main()
