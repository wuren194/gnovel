"""P7 delivery acceptance: external intake, installer reality, package reality.

Complements ``test_end_to_end_delivery.py`` (which drives CLI/MCP/HTTP parity and
Doctor attribution). This module covers the boundaries that only exist at the
edges of the toolbox:

* External Manuscript intake — the registered transport exception
* Installer — idempotent, recoverable, Unicode-safe, never a half-install
* Local package — auditable manifest, private/credential exclusion, tamper detection
* Canon → Impact → stale Context with current source bindings
* Skill / Sepia authorization boundaries
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.core.jsonio import write_json  # noqa: E402
from scripts.core.structure_store import load_structure, save_structure  # noqa: E402
from tests.fixtures_source import source_fixture  # noqa: E402


class ExternalManuscriptIntakeTests(unittest.TestCase):
    """Sample H: .txt/.md → evidence review → boundary candidates → no writable truth."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "外来稿"
        self.source.mkdir()
        (self.source / "序.md").write_text("# 序\n\n他一进门就看见那把椅子。\n", encoding="utf-8")
        (self.source / "第一章 椅子.txt").write_text(
            "第一章 椅子\n\n他坐下来，手指敲着扶手。\n\n第二章 离开\n\n他起身走了。\n", encoding="utf-8")
        self.target = self.base / "证据副本"

    def review(self, source=None, target=None):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "novelctl.py"), "project",
                               "import", "review", "--source", str(source or self.source),
                               "--target-root", str(target or self.target)],
                              cwd=ROOT, text=True, capture_output=True, timeout=120)
        return proc.returncode, json.loads(proc.stdout)

    def test_intake_produces_evidence_only_review_with_boundary_candidates(self):
        code, payload = self.review()
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["code"], "EXTERNAL_MANUSCRIPT_REVIEW_CREATED")
        self.assertFalse(payload["writableStoryTruthEnabled"])
        self.assertEqual(payload["sourceFilesModified"], False)
        self.assertGreaterEqual(payload["fileCount"], 2)
        self.assertGreaterEqual(payload["chapterHintCount"], 2)
        # 机器清单才是边界候选的真源；它必须逐条带未批准标记与字节定位。
        inventory_path = self.target / "external-manuscript-inventory.json"
        self.assertTrue(inventory_path.is_file(),
                        "证据副本必须落一份机器可读清单，否则 CLI 摘要无从核对")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        self.assertFalse(inventory["writableStoryTruthEnabled"])
        self.assertEqual(inventory["candidateContract"]["defaultStatus"], "unapproved")
        self.assertFalse(inventory["candidateContract"]["formalSource"])
        self.assertTrue(inventory["chapterBoundaryCandidates"])
        for candidate in inventory["chapterBoundaryCandidates"]:
            with self.subTest(candidate=candidate["candidateId"]):
                self.assertEqual(candidate["status"], "unapproved")
                self.assertFalse(candidate["formalSource"])
                self.assertTrue(candidate["sourceFileHash"])
                self.assertIn("byteStart", candidate["sourceLocator"])
                self.assertIn("byteEnd", candidate["sourceLocator"])
        # 证据副本里不得出现可写结构真源。
        self.assertFalse((self.target / ".govel" / "structure.json").exists())
        self.assertTrue((self.target / "evidence").is_dir())
        self.assertEqual(payload["inventoryHash"], inventory["inventoryHash"])

    def test_intake_is_the_registered_transport_exception_not_a_shadow_entry(self):
        registry = json.loads((ROOT / "shared" / "registries" / "tools.json").read_text(encoding="utf-8"))
        entry = next((row for row in registry["entrypoints"]
                      if row["id"] == "external-manuscript-intake"), None)
        self.assertIsNotNone(entry, "外部稿件入库必须是已登记入口，不是影子")
        self.assertIn("import review", entry["transport"])
        self.assertIn("transport exception", entry["notes"])
        self.assertIn("writableStoryTruthEnabled", entry["surface"])
        # 已申报的例外：不进 MCP tool list，也不进 dashboard 路由。
        from scripts.mcp_server import TOOLS
        names = {row["name"] for row in TOOLS}
        self.assertNotIn("create_external_manuscript_review", names)
        self.assertNotIn("import_review", names)
        server = (ROOT / "scripts" / "dashboard_server.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/import", server)

    def test_intake_refuses_symlink_source_and_occupied_target_without_touching_them(self):
        link = self.base / "链接稿.md"
        link.symlink_to(self.source / "序.md")
        code, payload = self.review(source=link)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["code"], "EXTERNAL_MANUSCRIPT_SOURCE_INVALID")

        occupied = self.base / "已占用"
        occupied.mkdir()
        (occupied / "已有文件.md").write_text("别动我", encoding="utf-8")
        code, payload = self.review(target=occupied)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["code"], "EXTERNAL_MANUSCRIPT_TARGET_INVALID")
        self.assertEqual((occupied / "已有文件.md").read_text(encoding="utf-8"), "别动我")

    def test_intake_handles_unicode_and_space_paths(self):
        spaced = self.base / "带 空格 的目录"
        spaced.mkdir()
        (spaced / "外来 稿件.md").write_text("# 标题\n\n内容。\n", encoding="utf-8")
        target = self.base / "副本 目录"
        code, payload = self.review(source=spaced, target=target)
        self.assertEqual(code, 0, payload)
        self.assertTrue((target / "evidence").is_dir())


class InstallerRealityTests(unittest.TestCase):
    """The installer must be idempotent, recoverable, Unicode-safe and never half-done.

    Every test runs against a throwaway HOME: the real ``~/.gemini`` is never
    touched, so "second install is a no-op" can be asserted rather than assumed.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "家"
        self.gemini = self.home / ".gemini"
        (self.gemini / "config" / "skills").mkdir(parents=True)
        self.skill_root = self.gemini / "config" / "skills" / "govel"
        self.original_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        importlib.reload(importlib.import_module("scripts.install_antigravity"))

    def _restore_home(self):
        if self.original_home is not None:
            os.environ["HOME"] = self.original_home

    def installer(self):
        import scripts.install_antigravity as module
        return module

    def test_clean_install_then_repeat_is_a_true_no_op(self):
        module = self.installer()
        first = module.install()
        self.assertTrue(first["ok"], first["problems"])
        created = [row for row in first["actions"] if row.startswith("linked:")]
        self.assertTrue(created)
        self.assertTrue((self.skill_root / "SKILL.md").is_symlink())

        second = module.install()
        self.assertTrue(second["ok"], second["problems"])
        self.assertEqual([row for row in second["actions"] if row.startswith("linked:")], [],
                         "第二次安装不得重建任何软链，否则它的动作日志证明不了幂等")
        self.assertTrue(all(row.startswith("already linked:") for row in second["actions"]))

    def test_wrong_symlink_and_missing_link_are_both_repaired(self):
        module = self.installer()
        module.install()
        (self.skill_root / "SKILL.md").unlink()
        (self.skill_root / "docs").unlink()
        stray = self.base / "无关目标"
        stray.mkdir()
        import shutil
        shutil.rmtree(self.skill_root)
        self.skill_root.symlink_to(stray, target_is_directory=True)
        report = module.install()
        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue((self.skill_root / "SKILL.md").is_symlink())
        self.assertTrue((self.skill_root / "docs").is_symlink())
        self.assertEqual(module.check()["fixes"], [])

    def test_pre_existing_real_directory_is_not_overwritten_without_force(self):
        module = self.installer()
        module.install()
        victim = self.skill_root / "docs"
        victim.unlink()
        victim.mkdir()
        (victim / "作者自己写的.md").write_text("我的东西", encoding="utf-8")
        blocked = module.install()
        self.assertFalse(blocked["ok"])
        self.assertTrue(any("安全阻断" in row for row in blocked["problems"]))
        self.assertEqual((victim / "作者自己写的.md").read_text(encoding="utf-8"), "我的东西")

        forced = module.install(force=True)
        self.assertTrue(forced["ok"], forced["problems"])
        self.assertTrue(victim.is_symlink())
        backups = list((self.gemini / "config" / "skills").glob(".trash-*"))
        self.assertTrue(backups, "覆盖前必须留下可恢复备份")
        self.assertTrue(backups, "覆盖前必须留下可恢复备份")
        self.assertTrue(any(list(path.rglob("作者自己写的.md")) for path in backups),
                        "备份必须真的含被移走的作者文件")

    def test_missing_parent_directory_is_reported_not_created_blindly(self):
        import shutil
        shutil.rmtree(self.gemini)
        module = self.installer()
        report = module.install()
        self.assertFalse(report["ok"])
        self.assertTrue(report["problems"])
        self.assertFalse(self.skill_root.exists(), "父目录不存在时不得留下半安装")

    def test_unicode_and_space_paths_work(self):
        spaced = self.base / "带 空格 的家"
        (spaced / ".gemini" / "config" / "skills").mkdir(parents=True)
        os.environ["HOME"] = str(spaced)
        module = importlib.reload(importlib.import_module("scripts.install_antigravity"))
        report = module.install()
        self.assertTrue(report["ok"], report["problems"])
        root = spaced / ".gemini" / "config" / "skills" / "govel"
        self.assertTrue((root / "SKILL.md").is_symlink())
        self.assertTrue((root / "knowledge").is_symlink())

    def test_obsolete_entry_is_removed_but_a_real_one_needs_force(self):
        module = self.installer()
        module.install()
        obsolete = self.gemini / "config" / "skills" / "govel-setup"
        obsolete.mkdir()
        (obsolete / "旧文件.md").write_text("作废", encoding="utf-8")
        blocked = module.install()
        self.assertFalse(blocked["ok"])
        self.assertTrue((obsolete / "旧文件.md").is_file())
        forced = module.install(force=True)
        self.assertTrue(forced["ok"], forced["problems"])
        self.assertFalse(obsolete.exists())


class LocalPackageTests(unittest.TestCase):
    """The package must be auditable: content == manifest, and nothing private leaks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.out = self.base / "dist"
        import scripts.package_toolbox as packager
        self.packager = packager

    def packager_module(self):
        return importlib.import_module("scripts.package_toolbox")

    def test_public_plan_includes_chosen_license_and_notices(self):
        module = self.packager_module()
        manifest = module.build_manifest(
            mode="public-audit",
            local_paths=module._local_path_references(module.collect()["packaged"]),
        )
        readiness = manifest["readiness"]
        self.assertTrue(readiness["localPersonalReleaseReady"])
        self.assertTrue(readiness["publicReleaseReady"],
                        readiness["publicReleaseBlockers"])
        self.assertTrue(readiness["rootLicensePresent"])
        self.assertEqual(readiness["license"], "ROOT_LICENSE_PRESENT")
        self.assertEqual(readiness["publicReleaseBlockers"], [])
        packaged = {row["path"] for row in manifest["files"]}
        self.assertTrue(
            {"LICENSE", "THIRD_PARTY_NOTICES.md", "LICENSES/sepia-MIT.txt"} <= packaged
        )

    def test_private_and_credential_paths_are_excluded_by_category(self):
        module = self.packager_module()
        cases = {
            "小说参考/某书.md": "private_reference_alias",
            "knowledge/sources/private/原文.txt": "private_source",
            "knowledge/.cache/search.sqlite3": "derived_private_state",
            ".env": "credential_candidate",
            "config/id_rsa": "credential_candidate",
            "secrets/tls.pem": "credential_candidate",
            ".git/config": "vcs_metadata",
            "dist/old.zip": "build_artifact",
            "pkg/__pycache__/x.pyc": "bytecode",
            "nested/.DS_Store": "finder_noise",
            "../escape.md": "unsafe_path",
            "normal/readme.md": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(module._unsafe(name), expected)

    def test_directory_package_matches_its_manifest_and_detects_tampering(self):
        module = self.packager_module()
        manifest = module.build_manifest(mode="local-personal",
                                         local_paths=module._local_path_references(module.collect()["packaged"]))
        module.build_directory(self.out, manifest)
        verified = module.check(self.out / module.MANIFEST_NAME, None)
        self.assertTrue(verified["ok"], verified["problems"][:5])

        # 改一个字节：校验必须抓到，不能仍报成功。
        victim = next(row["path"] for row in manifest["files"]
                      if not (self.out / row["path"]).is_symlink())
        target = self.out / victim
        target.write_bytes(target.read_bytes() + b"tampered")
        tampered = module.check(self.out / module.MANIFEST_NAME, None)
        self.assertFalse(tampered["ok"])
        self.assertIn("hash_mismatch", {row["code"] for row in tampered["problems"]})

    def test_extra_and_missing_files_fail_the_manifest_check(self):
        module = self.packager_module()
        manifest = module.build_manifest(mode="local-personal", local_paths={})
        module.build_directory(self.out, manifest)
        (self.out / "偷偷带进来的.md").write_text("x", encoding="utf-8")
        first = next(row["path"] for row in manifest["files"])
        (self.out / first).unlink()
        report = module.check(self.out / module.MANIFEST_NAME, None)
        self.assertFalse(report["ok"])
        codes = {row["code"] for row in report["problems"]}
        self.assertIn("package_file_not_in_manifest", codes)
        self.assertIn("manifest_file_missing", codes)

    def test_leaking_a_private_file_into_the_package_fails_the_check(self):
        module = self.packager_module()
        manifest = module.build_manifest(mode="local-personal", local_paths={})
        module.build_directory(self.out, manifest)
        leak = self.out / "knowledge" / "sources" / "private" / "原文.txt"
        leak.parent.mkdir(parents=True, exist_ok=True)
        leak.write_text("私有原文", encoding="utf-8")
        report = module.check(self.out / module.MANIFEST_NAME, None)
        self.assertFalse(report["ok"])
        self.assertIn("excluded_category_present", {row["code"] for row in report["problems"]})

    def test_zip_package_verifies_and_reports_the_manifest(self):
        module = self.packager_module()
        manifest = module.build_manifest(mode="local-personal",
                                         local_paths=module._local_path_references(module.collect()["packaged"]))
        archive = self.base / "toolbox.zip"
        module.build_zip(archive, manifest)
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as handle:
            self.assertIn(module.MANIFEST_NAME, handle.namelist())
        verified = module.check(self.base / "toolbox.zip.MANIFEST.json", archive)
        self.assertTrue(verified["ok"], verified["problems"][:5])

    def test_release_contents_checker_agrees_with_the_packager(self):
        """两个入口不得对同一棵树给出相反结论。"""
        module = self.packager_module()
        collected = module.collect()
        self.assertNotIn("knowledge/sources/private/", "".join(collected["packaged"]))
        report = importlib.import_module("check_release_contents").check_worktree()
        self.assertTrue(report["ok"], report["problems"][:5])


class PackagingReadinessMatrixTests(unittest.TestCase):
    """build 成功 / 本地就绪 / 公开发布就绪是三级判定，互不冒充；不安全软链不得被打包。

    机制版本 95：旧实现用 resolve() 判定软链、却在 --check 里用原样 target 判定，
    同一产物 build 后立刻被自己的 check 拒绝；localPersonalReleaseReady 也不看证据、
    恒为 true。现在 collect 与 check 共用同一条 archive 规则，就绪由证据计算。
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.packager = importlib.import_module("scripts.package_toolbox")
        self.candidates: list[str] = []
        self._restore: list[tuple[str, object]] = []
        for attribute, value in (("TOOLBOX_ROOT", self.root),
                                 ("_candidates", lambda: list(self.candidates)),
                                 ("toolbox_manifest", lambda: {"version": "test"})):
            self._restore.append((attribute, getattr(self.packager, attribute)))
            setattr(self.packager, attribute, value)

        def restore():
            for attribute, original in reversed(self._restore):
                setattr(self.packager, attribute, original)

        self.addCleanup(restore)

    def write(self, name: str, content: str = "内容") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if name not in self.candidates:
            self.candidates.append(name)
        return path

    def link(self, name: str, target: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        self.candidates.append(name)
        return path

    def build(self):
        manifest = self.packager.build_manifest(mode="local-personal", local_paths={})
        out = self.root / "dist-out"
        report = self.packager.build_directory(out, manifest)
        verified = self.packager.check(out / self.packager.MANIFEST_NAME, None)
        return manifest, report, verified

    def packaged(self, manifest) -> set:
        return {row["path"] for row in manifest["files"]}

    def test_clean_tree_is_locally_ready_and_only_public_is_blocked(self):
        self.write("README.md")
        manifest, report, verified = self.build()
        self.assertEqual(report["code"], "PACKAGE_BUILT")
        self.assertTrue(verified["ok"], verified["problems"])
        readiness = manifest["readiness"]
        self.assertTrue(readiness["localPersonalReleaseReady"])
        self.assertEqual(readiness["localReleaseBlockers"], [])
        self.assertFalse(readiness["publicReleaseReady"])
        self.assertEqual({row["code"] for row in readiness["publicReleaseBlockers"]},
                         {"PUBLIC_RELEASE_LICENSE_UNDECIDED"})

    def test_private_and_legacy_sources_do_not_impersonate_readiness_levels(self):
        self.write("README.md")
        self.write("knowledge/sources/private/raw.txt", "私有原文")
        self.write("knowledge/sources/legacy/KN-1.md", "旧来源")
        manifest, report, verified = self.build()
        self.assertEqual(report["code"], "PACKAGE_BUILT")
        packaged = self.packaged(manifest)
        self.assertNotIn("knowledge/sources/private/raw.txt", packaged, "私有原文不得进入任何包")
        self.assertIn("knowledge/sources/legacy/KN-1.md", packaged)
        self.assertEqual(manifest["excluded"]["knowledge/sources/private/raw.txt"]["category"],
                         "private_source")
        readiness = manifest["readiness"]
        self.assertTrue(readiness["localPersonalReleaseReady"], "旧来源只挡公开发布，不挡本地私用")
        self.assertEqual(readiness["localReleaseBlockers"], [])
        self.assertFalse(readiness["publicReleaseReady"])
        public_codes = {row["code"] for row in readiness["publicReleaseBlockers"]}
        self.assertIn("SOURCE_RIGHTS_NOT_CLEARED", public_codes)
        self.assertNotIn("SOURCE_RIGHTS_NOT_CLEARED",
                         {row["code"] for row in readiness["localReleaseBlockers"]},
                         "公开 blocker 不得冒充本地 blocker")
        self.assertTrue(verified["ok"], verified["problems"])

    def test_unsafe_symlinks_are_excluded_not_packaged_and_build_still_stands(self):
        self.write("note.md")
        self.write("knowledge/sources/private/hidden.md", "私密内容")
        self.candidates.remove("knowledge/sources/private/hidden.md")  # 模拟 Git 忽略：磁盘存在、非候选
        self.link("alias-abs.md", str(self.root / "note.md"))
        self.link("alias-private.md", "knowledge/sources/private/hidden.md")
        manifest, report, verified = self.build()
        self.assertEqual(report["code"], "PACKAGE_BUILT", "排除不安全软链不是 build 失败")
        self.assertFalse({"alias-abs.md", "alias-private.md"} & self.packaged(manifest),
                         "不安全软链不得被打包")
        for name in ("alias-abs.md", "alias-private.md"):
            self.assertEqual(manifest["excluded"][name]["category"], "external_or_broken_symlink")
        self.assertTrue(verified["ok"], verified["problems"])
        self.assertTrue(manifest["readiness"]["localPersonalReleaseReady"])

    def test_link_chains_must_terminate_on_packaged_content(self):
        self.write("note.md")
        self.link("hop.md", "mid.md")
        self.link("mid.md", "note.md")
        self.link("bad-hop.md", "bad-mid.md")
        self.link("bad-mid.md", "gone.md")
        manifest, _, verified = self.build()
        packaged = self.packaged(manifest)
        self.assertTrue({"hop.md", "mid.md"} <= packaged, "可自洽的链必须保留")
        self.assertFalse({"bad-hop.md", "bad-mid.md"} & packaged, "断链的链必须整体排除")
        self.assertTrue(verified["ok"], verified["problems"])

    def test_public_ready_requires_license_and_clean_rights_together(self):
        self.write("LICENSE", "MIT License")
        self.write("README.md")
        manifest, _, verified = self.build()
        self.assertTrue(verified["ok"], verified["problems"])
        readiness = manifest["readiness"]
        self.assertTrue(readiness["localPersonalReleaseReady"])
        self.assertEqual(readiness["localReleaseBlockers"], [])
        self.assertTrue(readiness["publicReleaseReady"])
        self.assertEqual(readiness["publicReleaseBlockers"], [])
        self.assertEqual(readiness["license"], "ROOT_LICENSE_PRESENT")
        archive = self.root / "toolbox.zip"
        self.packager.build_zip(archive, manifest)
        from scripts.check_release_contents import check_zip
        self.assertTrue(check_zip(archive, public=True)["ok"], "公开就绪的包必须通过公开发布口径校验")


class SkillAndSepiaBoundaryTests(unittest.TestCase):
    """Skill stays a task description; Sepia keeps explicit authorization gates."""

    STATIONS = ("composer", "planner", "writer", "editor", "titler", "router")

    def test_no_station_inlines_runtime_business_rules(self):
        banned = ("knowledge ranking", "context assembly", "invalidation", "CAS",
                  "migration ready 判定", "canon guard")
        for station in self.STATIONS:
            text = (ROOT / f"govel-{station}" / "SKILL.md").read_text(encoding="utf-8")
            for phrase in banned:
                with self.subTest(station=station, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_no_station_references_a_deleted_command(self):
        """六个工位引用的每条 novelctl 命令都必须真实存在。

        复用 test_documented_cli_exists 的同一条反射口径：文档漂移曾经让照文档执行
        必然报错，而人工同步没拦住。这里把工位 SKILL 单独钉一遍，因为它们是被
        复制进客户端、被执行最多的入口。
        """
        import argparse
        import re

        from novelctl import build_parser

        def walk(parser, prefix=()):
            groups = [action for action in parser._actions
                      if isinstance(action, argparse._SubParsersAction)]
            if not groups:
                return {" ".join(prefix)} if prefix else set()
            found = set()
            for action in groups:
                for name, subparser in action._name_parser_map.items():
                    found |= walk(subparser, prefix + (name,))
            return found

        real = walk(build_parser())
        prefixes = {" ".join(parts[:size]) for command in real
                    for parts in [command.split()] for size in range(1, len(parts) + 1)}
        invocation = re.compile(r"novelctl(?:\.py)?((?:\s+[a-z][a-z-]*)+)")
        for station in self.STATIONS:
            text = (ROOT / f"govel-{station}" / "SKILL.md").read_text(encoding="utf-8")
            for match in invocation.finditer(text):
                command = match.group(1).strip()
                with self.subTest(station=station, command=command):
                    self.assertIn(command, prefixes,
                                  f"{station} 引用了不存在的命令 {command}")



    def test_editor_keeps_both_write_more_and_prevent_over_organization(self):
        editor = (ROOT / "govel-editor" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("G-EXPANSION.md", editor, "字数不足必须走扩写，不许反 AI 味就把文本越删越短")
        self.assertIn("G-OVER-ORGANIZATION.md", editor, "组织过满是独立方向，两个方向都要保留")


class CanonImpactAcceptanceTests(unittest.TestCase):
    """Current Canon → Impact → stale Context; native format upgrades are retired."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".govel").mkdir()
        source = source_fixture(self.root)
        write_json(self.root / ".govel/project.json", {
            "schemaVersion": 5, "workflowVersion": "5.0", "projectId": source["projectId"],
            "bookName": source["bookName"], "approvalMode": "manual"})
        save_structure(self.root, source)

    def test_canon_proposal_changes_context_hash_and_invalidates_the_old_one(self):
        from scripts.core.canon_proposals import resolve_canon_target, preview_canon_change
        from scripts.core.structure_commands import commit_plan
        source = load_structure(self.root)
        resolved = resolve_canon_target(self.root, selector="人物甲")
        session_ref = resolved["sessionRef"]
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(len(resolved["matches"]), 1)
        entity_id = resolved["matches"][0]["entityId"]
        current = next(row for row in source["entities"] if row["entityId"] == entity_id)
        preview = preview_canon_change(
            self.root, sessionRef=session_ref, fieldPath="summary",
            expectedOldExists=current.get("summary") is not None,
            expectedOldValue=current.get("summary"), newValue="他其实怕高。",
            reason="补一条叙事事实",
            evidence=[{"fragmentId": "SF-HEIGHT", "quote": "他身高一米九。"}],
            approvalActor="作者", approvalEvidence="当面确认")
        self.assertEqual(preview["operation"], "canon_change_proposal")
        commit_plan(self.root, operation_id=preview["operationId"],
                    plan_hash=preview["planHash"], approved=True)
        after = load_structure(self.root)
        entity = next(row for row in after["entities"] if row["entityId"] == entity_id)
        self.assertEqual(entity["summary"], "他其实怕高。")
        # 旧快照的 sourceHash 必须与新真源不同，否则旧上下文会被当成仍然有效。
        from scripts.core.source_model import snapshot_hash
        self.assertNotEqual(snapshot_hash(source), snapshot_hash(after))

    def test_stale_evidence_rejects_the_proposal(self):
        from scripts.core.canon_proposals import resolve_canon_target, preview_canon_change
        from scripts.core.structure_store import StructureError
        source = load_structure(self.root)
        resolved = resolve_canon_target(self.root, selector="人物甲")
        # 解析之后改动真源：同一个 sessionRef 必须失效，而不是照着旧值写进去。
        mutated = load_structure(self.root)
        next(row for row in mutated["entities"]
             if row["entityId"] == source["entities"][0]["entityId"])["summary"] = "被改过"
        write_json(self.root / ".govel" / "structure.json", mutated)
        with self.assertRaises(StructureError) as error:
            preview_canon_change(
                self.root, sessionRef=resolved["sessionRef"], fieldPath="summary",
                expectedOldExists=True, expectedOldValue="", newValue="新值", reason="改",
                evidence=[{"fragmentId": "SF-HEIGHT", "quote": "他身高一米九。"}],
                approvalActor="作者", approvalEvidence="确认")
        self.assertEqual(error.exception.code, "SESSION_REF_STALE")




# Execute every toolbox mutation (including CLI children) in an owned physical copy.
from tests.toolbox_isolation import install_isolation
install_isolation(globals())

if __name__ == "__main__":
    unittest.main()
