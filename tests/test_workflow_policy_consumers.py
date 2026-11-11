"""Workflow-policy fields must have real consumers.

动机：`workflow.json` 的 approval.defaultMode / serialPlanning.* / protectedPaths
曾经零消费者——改值不改变任何行为，等于伪配置；`protectedPaths` 还与
manifest.agileWorkbench.protectedCheckoutPaths 内容重复而无人读取，
`chapterLength` 则在 scripts/core/commands.py 里被硬编码成第二份真源。
RUNTIME-CONFIG 明令「脚本不得在多个模块重复硬编码同一阈值」「报告必须包含
有效配置来源」，本测试把这两条变成可执行断言。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.project import (
    chapter_length_bounds,
    check_layout_invariants,
    default_approval_mode,
    gc_keep_backups_default,
    protected_paths,
    serial_planning_policy,
    state_compaction_policy,
    structure_batch_max_steps,
    canon_proposal_batch_max_items,
    toolbox_manifest,
    workflow_policy,
)

ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"
POLICY_PATH = ROOT / "shared" / "registries" / "workflow.json"
MANIFEST_PATH = ROOT / "toolbox.manifest.json"


def run_cli(args: list[str]) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(NOVELCTL), *args], cwd=ROOT, capture_output=True, text=True
    )
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {"code": "NON_JSON", "stdout": proc.stdout, "stderr": proc.stderr}


def init_project(root: Path) -> None:
    code, result = run_cli(
        ["project", "quick-init", "--project-root", str(root), "--book-name", "策略测试", "--no-agents-md"]
    )
    assert code == 0, result


class PolicyAccessorTests(unittest.TestCase):
    def test_accessors_read_the_registry(self) -> None:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(default_approval_mode(), policy["approval"]["defaultMode"])
        self.assertEqual(protected_paths(), policy["protectedPaths"])
        self.assertEqual(serial_planning_policy(), policy["serialPlanning"])
        self.assertEqual(
            chapter_length_bounds(),
            (policy["chapterLength"]["minHanCharacters"], policy["chapterLength"]["maxHanCharacters"]),
        )
        # 机制版本 36 接入的维护阈值：访问器值必须与注册表一致（改注册表即改行为）。
        self.assertEqual(gc_keep_backups_default(), policy["maintenance"]["structureGcKeepBackups"])
        self.assertEqual(structure_batch_max_steps(), policy["maintenance"]["structureBatchMaxSteps"])
        self.assertEqual(canon_proposal_batch_max_items(), policy["maintenance"]["canonProposalBatchMaxItems"])
        self.assertEqual(
            state_compaction_policy(),
            {
                "threshold": policy["maintenance"]["stateCompaction"]["threshold"],
                "oldChapterThreshold": policy["maintenance"]["stateCompaction"]["oldChapterThreshold"],
            },
        )
        check_layout_invariants()

    def test_chapter_length_is_not_hardcoded_in_command_module(self) -> None:
        source = (ROOT / "scripts/core/commands.py").read_text(encoding="utf-8")
        self.assertIn("chapter_length_bounds", source, "commands.py 必须从注册表读区间")
        for stale in ("8500", "12000", "CHAPTER_LENGTH_MIN", "CHAPTER_LENGTH_MAX"):
            self.assertNotIn(stale, source, f"commands.py 残留硬编码字数：{stale}")

    def test_approval_mode_default_is_not_hardcoded_in_argparse(self) -> None:
        source = (ROOT / "scripts/novelctl.py").read_text(encoding="utf-8")
        self.assertNotIn(
            '"--approval-mode", default="manual"',
            source,
            "argparse 不得写死审批默认值，真源是 workflow.json.approval.defaultMode",
        )
        self.assertIn("default_approval_mode()", source)


class PolicyIsEnforcedTests(unittest.TestCase):
    """字段必须能改变行为或拦下问题，否则仍是伪配置。"""

    def _patched(self, path: Path, mutate) -> str:
        original = path.read_text(encoding="utf-8")
        data = json.loads(original)
        mutate(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return original

    def test_chapter_length_change_reaches_overview(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            init_project(root)
            original = self._patched(
                POLICY_PATH, lambda d: d["chapterLength"].__setitem__("minHanCharacters", 9999)
            )
            try:
                code, result = run_cli(["project", "overview", "--project-root", str(root)])
                self.assertEqual(code, 0, result)
                self.assertEqual(result["chapterLength"]["minHanCharacters"], 9999)
            finally:
                POLICY_PATH.write_text(original, encoding="utf-8")

    def test_doctor_reports_effective_policy_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            init_project(root)
            code, result = run_cli(["project", "doctor", "--project-root", str(root)])
            self.assertEqual(code, 0, result)
            policy = result["workflowPolicy"]
            self.assertEqual(policy["source"], "shared/registries/workflow.json")
            self.assertTrue(policy["valid"])
            self.assertEqual(policy["approvalMode"], default_approval_mode())
            self.assertEqual(policy["protectedPaths"], protected_paths())

    def test_doctor_flags_protected_path_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            init_project(root)
            original = self._patched(
                MANIFEST_PATH,
                lambda d: d["agileWorkbench"].__setitem__("protectedCheckoutPaths", ["正文"]),
            )
            try:
                code, result = run_cli(["project", "doctor", "--project-root", str(root)])
                self.assertNotEqual(code, 0, result)
                problems = result["details"]["problems"]
                self.assertTrue(
                    any("protected paths diverge" in item for item in problems), problems
                )
            finally:
                MANIFEST_PATH.write_text(original, encoding="utf-8")

    def test_doctor_fails_closed_on_missing_serial_planning_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            init_project(root)
            original = self._patched(
                POLICY_PATH, lambda d: d["serialPlanning"].pop("currentVolumeOnly")
            )
            try:
                code, result = run_cli(["project", "doctor", "--project-root", str(root)])
                self.assertNotEqual(code, 0, result)
                problems = result["details"]["problems"]
                self.assertTrue(
                    any("currentVolumeOnly" in item for item in problems), problems
                )
            finally:
                POLICY_PATH.write_text(original, encoding="utf-8")


class SingleSourceDeclarationTests(unittest.TestCase):
    def test_protected_paths_agree_across_declarations(self) -> None:
        manifest_paths = toolbox_manifest()["agileWorkbench"]["protectedCheckoutPaths"]
        self.assertEqual(
            list(manifest_paths),
            protected_paths(),
            "manifest 与 workflow.json 的受保护路径必须一致",
        )

    def test_manifest_declares_workflow_json_as_the_policy_source(self) -> None:
        manifest = toolbox_manifest()
        self.assertEqual(
            manifest["sharedProtocols"]["workflowPolicy"], "shared/registries/workflow.json"
        )
    def test_approval_scope_requires_contiguous_same_volume_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            init_project(root)
            structure = json.loads((root / ".govel/structure.json").read_text(encoding="utf-8"))
            # 通过结构事务插入三章，再把中间章从批准列表中省略。
            volume_id = structure["volumes"][0]["volumeId"]
            arc_id = structure["arcs"][0]["arcId"]
            ids = []
            for title in ("一", "二", "三"):
                code, preview = run_cli(["project", "structure", "insert-preview", "--project-root", str(root), "--title", title, "--volume-id", volume_id, "--primary-arc-id", arc_id])
                self.assertEqual(code, 0, preview)
                code, committed = run_cli(["project", "structure", "commit", "--project-root", str(root), "--operation-id", preview["operationId"], "--plan-hash", preview["planHash"], "--approve"])
                self.assertEqual(code, 0, committed)
                ids.append(committed["chapter"]["chapterId"])
            code, result = run_cli(["project", "workflow", "approve-chapters", "--project-root", str(root), "--chapter-id", ids[0], "--chapter-id", ids[2]])
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "APPROVAL_RANGE_NOT_CONTIGUOUS")
            code, allowed = run_cli([
                "project", "workflow", "approve-chapters", "--project-root", str(root),
                "--chapter-id", ids[0], "--chapter-id", ids[2],
                "--allow-gaps", "--reason", "只试写首尾两章",
            ])
            self.assertEqual(code, 0, allowed)
            self.assertTrue(allowed["approvalScope"]["allowGaps"])
            self.assertEqual(allowed["approvalScope"]["reason"], "只试写首尾两章")


class DeadConfigRemovalTests(unittest.TestCase):
    """workflow.knowledgeRouter 是审计确认的死配置（零生产消费者），机制版本 95 删除。

    行为消融证据在 `tests/test_knowledge_router.py::WorkflowRegistryAblationTests`：
    注入任意值不改变路由、打包与准备行为。这里锁住删除面，防止无消费者的字段回归。
    """

    def test_knowledge_router_config_stays_removed(self) -> None:
        self.assertNotIn("knowledgeRouter", workflow_policy())
        production = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted((ROOT / "scripts").rglob("*.py"))
        )
        self.assertNotIn("knowledgeRouter", production)
        self.assertNotIn("knowledge_router_policy", production)


class ProseQualityPolicyTests(unittest.TestCase):
    """Change the copy's real policy, then consume it through the real validator."""

    def test_threshold_boundary_and_registry_change_preserve_rating(self):
        policy_path = ROOT / 'shared/registries/text-patterns.json'
        original = policy_path.read_bytes()
        policy = json.loads(original)
        self.assertEqual(policy['diagnosticPolicy']['proseQuality']['minScore'], 70)
        bodies = {69: '与此同时' * 7 + '某种程度上',
                  70: '与此同时' * 6 + '某种程度上' * 2,
                  71: '与此同时' * 5 + '某种程度上' * 3}

        def check(body):
            process = subprocess.run([sys.executable, str(ROOT / 'scripts/validate-text.py'),
                                      '--mode', 'plot', '--intensity-level', '1', '--json'],
                                     cwd=ROOT, input=body, capture_output=True, text=True, timeout=20)
            payload = json.loads(process.stdout)
            return next(row for row in payload['checks'] if row['rule_id'] == 'S-10q')

        try:
            defaults = {score: check(body) for score, body in bodies.items()}
            self.assertEqual({score: row['pass'] for score, row in defaults.items()}, {69: True, 70: True, 71: True})
            self.assertTrue(all(row['level'] == 'info' for row in defaults.values()))
            for score, row in defaults.items():
                self.assertIn(f'得分 {score}/100', row['detail'])
            policy['diagnosticPolicy']['proseQuality']['minScore'] = 71
            policy['diagnosticPolicy']['proseQuality']['severity'] = 'fail'
            policy_path.write_text(json.dumps(policy, ensure_ascii=False))
            changed = check(bodies[70])
            self.assertFalse(changed['pass'])
            self.assertEqual(changed['detail'], defaults[70]['detail'])
            self.assertIn('等级:B', changed['detail'])
            self.assertEqual(changed['level'], 'fail')
        finally:
            # This is the module-owned copy; never a developer worktree restore.
            policy_path.write_bytes(original)

    def test_invalid_thresholds_fail_closed_in_real_policy_consumer(self):
        policy_path = ROOT / 'shared/registries/text-patterns.json'
        original = policy_path.read_bytes(); policy = json.loads(original)
        try:
            for invalid in (True, False, 0, 101, -1, 70.5, '70', None):
                with self.subTest(value=invalid):
                    policy['diagnosticPolicy']['proseQuality']['minScore'] = invalid
                    policy_path.write_text(json.dumps(policy, ensure_ascii=False))
                    process = subprocess.run([sys.executable, '-c',
                        'from scripts.core.project import diagnostic_policy; diagnostic_policy()'],
                        cwd=ROOT, capture_output=True, text=True, timeout=20)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn('diagnosticPolicy.proseQuality.minScore', process.stderr)
        finally:
            policy_path.write_bytes(original)



# Execute every toolbox mutation (including CLI children) in an owned physical copy.
from tests.toolbox_isolation import install_isolation
install_isolation(globals())

if __name__ == "__main__":
    unittest.main()
