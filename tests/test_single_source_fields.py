"""单一真源守卫：注册表/manifest 字段必须有消费者，或按规则被绑定/豁免。

动机：2026-09-20 审计发现 manifest 有 21 个字段零消费者（改值不改变任何行为，
即 RUNTIME-CONFIG 定义的伪配置），对应字面量被硬编码复制多处——schema 5 有 4 份、
UNSUPPORTED_PROJECT_VERSION 有 3 份、keep-backups 有 2 份。机制版本 36 把这些值
改为经访问器读取，本测试把审计方法变成永久守卫：

1. **消费规则**（纪律第 3 条）：被声明为真源的字段，必须在代码里被访问器读取，
   且访问器值与注册表逐字节一致；改注册表即改变行为——这是「改一处全局生效」的
   可执行证明。
2. **绑定规则**（纪律第 4 条）：布局不变量类字段（.gnovel、工作台）是文档常量，
   由 check_layout_invariants() 断言与机制常量一致，一侧漂移即 fail；反向的
   「断言被删空」由逐个篡改 manifest 的参数化测试锁住（M9 变异证明只有前者不够）。
3. **豁免规则**：文档性字段（_comment、version、skills、promptPlan 等）列在白名单
   里并写明理由；新增字段想进白名单必须在这里登记，让人在评审时看见。
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core import project
from scripts.core.project import (
    check_layout_invariants,
    current_schema_version,
    gc_keep_backups_default,
    unsupported_error_code,
    state_compaction_policy,
    supported_schema_versions,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "toolbox.manifest.json").read_text(encoding="utf-8"))
POLICY = json.loads((ROOT / "shared/registries/workflow.json").read_text(encoding="utf-8"))

# 源码树（scripts/ + tests/，排除缓存与归档），用于「消费者存在性」静态扫描。
SOURCE_FILES = [
    path
    for base in (ROOT / "scripts", ROOT / "tests")
    for path in base.rglob("*.py")
    if "__pycache__" not in path.parts and "archive" not in path.parts
]
PRODUCTION_SOURCES = {str(path.relative_to(ROOT)): path.read_text(encoding='utf-8')
                      for path in SOURCE_FILES if path.is_relative_to(ROOT / 'scripts')}

# schema 5 字面量的两种形态：
# - 比较形态（`== 5` / `!= 5`）覆盖 scripts/ 与 tests/ 全树；
# - 写入形态（`"schemaVersion": 5`）只覆盖源码树 scripts/——tests 里大量构造 schema 5
#   的 fixture 数据，那是测试输入而非真源漂移（机制 38 接线 state_cache/summary_status
#   后新增，此前这两种形态都不在守卫内）。
_SCHEMA_LITERAL = re.compile(r'schemaVersion[\"\'\)\]]*\s*[!=]=\s*5\b')
_SCHEMA_WRITE_LITERAL = re.compile(r'[\"\']schemaVersion[\"\']\s*:\s*5\b')


def _schema_literal_hits(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    hits = [line.strip() for line in lines if _SCHEMA_LITERAL.search(line)]
    if path.is_relative_to(ROOT / "scripts"):
        hits += [line.strip() for line in lines if _SCHEMA_WRITE_LITERAL.search(line)]
    return hits

# schema 5 字面量比较的显式豁免登记：只放「与项目 schema 无关的其它格式版本比较」，
# 值 = (理由, 该文件内此类比较的登记数量)。新增条目必须在这里登记，评审时可见。
# 曾按行内注释「与项目 schema 无关」豁免整行——那是自然语言子串匹配，任意一行补上
# 这七个字即可绕过守卫（机制 36 审计的变异 M6 借此在全部守卫测试上存活），故不认注释。
SCHEMA_LITERAL_EXEMPT_FILES = {
    "scripts/text/ai_patterns.py": ("文本规则注册表自身的格式版本，与项目 schema 无关", 1),
}

# Full paths only: another container's identically named leaf gains no exemption.
# These are descriptive protocol declarations, not purported runtime switches.
DOCUMENTED_ONLY_PATHS = {
    'name': 'Package identity; release manifest metadata.',
    'language': 'Describes the workstation language, not a runtime language toggle.',
    'schemaVersion': 'Bound to projectVersionPolicy by tests.test_architecture.',
    'workflowVersion': 'Current marker protocol; tests.test_architecture binds the declaration.',
    'compatibilityVersion': 'Every contract frontmatter is checked by tests.test_contract_frontmatter.',
    'sharedProtocols.contracts': 'Describes the fixed contract directory; contract frontmatter tests enumerate it.',
    'sharedProtocols.contractsList': 'tests.test_contract_frontmatter and tests.test_prompt_plan bind files and loading.',
    'execution.mode': 'Declares the single-track product contract; not a mode selector.',
    'execution.humanApprovalRequired': 'Describes the author-authorization contract, not a bypass option.',
    'agileWorkbench.persistent': 'Describes disposable workbench storage semantics.',
    'humanApproval.requiredAtPhase': 'Describes the protected-write approval boundary.',
    'humanApproval.command': 'Null declaration: no generic approval shell command.',
    'humanApproval.storage': 'Describes approval evidence ownership.',
    'humanApproval.actorType': 'Describes author responsibility; does not authenticate a human.',
    'humanApproval.interactiveOnly': 'Authoring protocol instruction, not a machine authentication claim.',
    'humanApproval.decisions': 'Declared protocol vocabulary for author decisions.',
    'humanApproval.shaBindings': 'Documents the exact artifact bindings enforced by finalization_preflight.',
    'corpusRoots.catalog': 'Documents the corpus catalog boundary; not an arbitrary external path option.',
    'corpusRoots.activeWritingReferences': 'Documents knowledge/assets as the writing reference source.',
    'promptPlan.gnovel-writer._note': 'Human-readable explanation of Writer loading.',
}

# Aliased/dynamic reads cannot be recognized as one AST access chain. Register
# their exact path, owning production file and required literal; the behavior
# tests remain necessary evidence beyond this existence guard.
ALIASED_CONSUMERS = {
    'storage.projectMarker': ('scripts/core/project.py', 'projectMarker'),
    'storage.cache': ('scripts/core/project.py', 'cache'),
    'storage.aux': ('scripts/core/project.py', 'aux'),
    'sharedProtocols.sourceModel': ('scripts/core/project.py', 'sourceModel'),
    'sharedProtocols.projectTemplates.agentsMd': ('scripts/core/project.py', 'projectTemplates'),
}


def _manifest_accesses(source):
    def access(node):
        if isinstance(node, ast.Name) and node.id in {'manifest', 'MANIFEST'}:
            return ()
        if isinstance(node, ast.Attribute) and node.attr == 'manifest':
            return ()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'toolbox_manifest':
            return ()
        parent = key = None
        if isinstance(node, ast.Subscript):
            parent, key = node.value, node.slice
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == 'get' and node.args):
            parent, key = node.func.value, node.args[0]
        if parent is not None and isinstance(key, ast.Constant) and isinstance(key.value, str):
            prefix = access(parent)
            if prefix is not None:
                return (*prefix, key.value)
        return None
    return {path for node in ast.walk(ast.parse(source)) if (path := access(node))}


def _literal_exists(source, key):
    return any(isinstance(node, ast.Constant) and node.value == key for node in ast.walk(ast.parse(source)))


def phantom_manifest_fields(manifest, sources):
    accesses = set().union(*(_manifest_accesses(source) for source in sources.values()))
    missing = []
    for key in sorted(_leaf_keys(manifest)):
        if key in DOCUMENTED_ONLY_PATHS or tuple(key.split('.')) in accesses:
            continue
        owner = ALIASED_CONSUMERS.get(key)
        if owner and owner[0] in sources and _literal_exists(sources[owner[0]], owner[1]):
            continue
        parts = key.split('.')
        dynamic_owner = None
        if parts[0] == 'classification' and len(parts) == 2:
            dynamic_owner = ('scripts/core/pair_classification.py', 'classification')
        elif parts[0] == 'onDemandReferences' and len(parts) == 2 and parts[1] in manifest.get('skills', []):
            dynamic_owner = ('scripts/core/prompt_plan.py', 'onDemandReferences')
        elif parts[0] == 'promptPlan' and len(parts) >= 3 and parts[1] in manifest.get('skills', []):
            suffix = '.'.join(parts[2:])
            if suffix in {'always', 'conditional.contract', 'conditional.reason', 'conditional.when.field',
                          'conditional.when.op', 'conditional.when.value'}:
                dynamic_owner = ('scripts/core/prompt_plan.py', 'promptPlan')
        elif parts[0] == 'promptSlices' and len(parts) >= 4 and parts[1] in manifest.get('skills', []) and parts[2] == 'default':
            contract = '.'.join(parts[3:])
            if contract in manifest.get('sharedProtocols', {}).get('contractsList', []):
                dynamic_owner = ('scripts/core/prompt_plan.py', 'promptSlices')
        if dynamic_owner and dynamic_owner[0] in sources and _literal_exists(sources[dynamic_owner[0]], dynamic_owner[1]):
            continue
        missing.append(key)
    return missing


def _leaf_keys(node: object, prefix: str = "") -> set[str]:
    if isinstance(node, dict):
        keys: set[str] = set()
        for key, value in node.items():
            if key == "_comment":
                continue
            keys |= _leaf_keys(value, f"{prefix}{key}.")
        return keys
    if isinstance(node, list):
        keys = set()
        for item in node:
            keys |= _leaf_keys(item, prefix)
        return keys
    return {prefix.rstrip(".")}


class ManifestSingleSourceTests(unittest.TestCase):
    def test_no_phantom_fields(self) -> None:
        """完整路径引用／具名动态消费者／有理由的协议声明，测试自引用不算消费。"""
        phantom = phantom_manifest_fields(MANIFEST, PRODUCTION_SOURCES)
        self.assertEqual(phantom, [], f"以下 manifest 字段零消费者（伪配置）：{phantom}")

    def test_phantom_guard_rejects_quotes_similar_names_and_wrong_path(self):
        manifest = {'storage': {'freshField': 1}}
        for source in ('x = "unrelated"', 'x = "freshFiel"', 'x = "freshField"',
                       'value = manifest["other"]["freshField"]',
                       '# manifest["storage"]["freshField"]\nvalue = 1'):
            with self.subTest(source=source):
                self.assertEqual(phantom_manifest_fields(manifest, {'scripts/probe.py': source}), ['storage.freshField'])
        for source in ('value = manifest["storage"]["freshField"]',
                       'value = toolbox_manifest().get("storage", {}).get("freshField")'):
            self.assertEqual(phantom_manifest_fields(manifest, {'scripts/probe.py': source}), [])

    def test_unknown_field_and_duplicate_leaf_cannot_borrow_known_consumer(self):
        for path, value in (('unusedUniqueManifestProbe', 1), ('other', {'cache': 'elsewhere'}),
                            ('defaultProjectRoot', '~/Desktop/小说')):
            mutant = deepcopy(MANIFEST); mutant[path] = value
            self.assertEqual(phantom_manifest_fields(mutant, PRODUCTION_SOURCES),
                             ['other.cache'] if path == 'other' else [path])

    def test_schema_version_has_single_source(self) -> None:
        """schema 版本只经访问器出现；源码不得再写字面量（比较形态 + 源码树写入形态，豁免须显式登记）。"""
        self.assertEqual(current_schema_version(), MANIFEST["projectVersionPolicy"]["supportedSchemaVersions"][0])
        self.assertEqual(supported_schema_versions(), [5])
        offenders = {}
        for path in SOURCE_FILES:
            relative = str(path.relative_to(ROOT))
            if relative in SCHEMA_LITERAL_EXEMPT_FILES:
                continue
            hits = _schema_literal_hits(path)
            if hits:
                offenders[relative] = hits
        self.assertEqual(
            offenders, {},
            f"以下文件把 schema 5 写成字面量；唯一真源是 manifest.supportedSchemaVersions: {offenders}",
        )

    def test_schema_literal_exemptions_stay_exact(self) -> None:
        """豁免登记必须仍然精确：条目对应文件的字面量增删，都要在此改登记。"""
        for relative, (reason, expected) in SCHEMA_LITERAL_EXEMPT_FILES.items():
            path = ROOT / relative
            self.assertTrue(path.is_file(), f"豁免登记指向不存在的文件：{relative}")
            hits = _schema_literal_hits(path)
            self.assertEqual(
                len(hits), expected,
                f"{relative} 的 schema 5 字面量数量与登记不符（登记 {expected}，实际 {len(hits)}）："
                f"已改用访问器就删登记，确需新增就先评审（登记理由：{reason}）",
            )

    def test_unsupported_error_code_has_single_source(self) -> None:
        self.assertEqual(unsupported_error_code(), "UNSUPPORTED_PROJECT_VERSION")
        offenders = [
            str(path.relative_to(ROOT))
            for path in SOURCE_FILES
            if path.parent.name != "tests"  # 测试可以断言错误码字符串本身
            and '"UNSUPPORTED_PROJECT_VERSION"' in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], f"以下文件硬编码了项目版本错误码字符串：{offenders}")

    def test_maintenance_thresholds_have_single_source(self) -> None:
        self.assertEqual(gc_keep_backups_default(), POLICY["maintenance"]["structureGcKeepBackups"])
        compaction = state_compaction_policy()
        self.assertEqual(compaction["threshold"], POLICY["maintenance"]["stateCompaction"]["threshold"])
        self.assertEqual(compaction["oldChapterThreshold"], POLICY["maintenance"]["stateCompaction"]["oldChapterThreshold"])
        import scripts.state_compactor as compactor

        self.assertEqual(compactor.COMPACTION_THRESHOLD, compaction["threshold"])
        self.assertEqual(compactor.OLD_CHAPTER_THRESHOLD, compaction["oldChapterThreshold"])


class HanCountSingleSourceTests(unittest.TestCase):
    """汉字计数曾有四套口径：text_utils 自称唯一真源，而 commands.py 另写
    `[一-鿿㐀-䶿]`（缺 〇 与扩展 B）——同一章 `project overview` 的字数与门禁/摘要
    不一致，lengthStatus 判定也会分叉（机制 39 接线）。"""

    def test_commands_reuses_text_utils_counter(self) -> None:
        from scripts import text_utils
        from scripts.core import commands

        self.assertIs(commands.chinese_char_count, text_utils.chinese_char_count)

    def test_no_local_han_range_literal_remains(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in SOURCE_FILES
            if path.parent.name != "tests" and "一-鿿" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], f"以下文件自带汉字区间字面量，须改用 text_utils.HAN_PATTERN：{offenders}")

    def test_counter_counts_rare_han_characters(self) -> None:
        from scripts.text_utils import chinese_char_count

        # 〇（U+3007）与扩展 B（𠀋）都在声明口径内；旧的 commands 口径少算这两个。
        self.assertEqual(chinese_char_count("他看见〇与㐀还有𠀋"), 9)

    def test_layout_invariants_hold(self) -> None:
        check_layout_invariants()

    def test_layout_invariants_reject_tampered_manifest(self) -> None:
        """逐个篡改 manifest 布局常量与代码常量，断言 check_layout_invariants fail closed。

        只「调用它、期望不抛」验证的是当前 manifest 合法，锁不住断言本身——把断言
        删空同样全绿（机制 36 审计的变异 M9 即此形态：4 条断言可被逐一删掉而不被
        察觉）。这里从反侧锁：每个字段被改坏都必须抛 ValueError。
        """
        from scripts.core import importer  # 延迟导入避免环（importer → project）

        def storage(key: str, value: object):
            return lambda manifest: manifest["storage"].update({key: value})

        def workbench(value: object):
            return lambda manifest: manifest["agileWorkbench"].update({"preferredPath": value})

        real_dirs = list(importer.REQUIRED_PROJECT_DIRS)
        cases = [
            ("未篡改的 manifest（对照：不抛）", None, None, False),
            ("storage.projectMarker 移出 .gnovel", storage("projectMarker", "notes"), None, True),
            ("storage.cache 移出 .gnovel", storage("cache", ".cache/state.json"), None, True),
            ("storage.aux 移出 .gnovel", storage("aux", "aux"), None, True),
            ("storage.projectMarker 不是字符串", storage("projectMarker", 5), None, True),
            ("agileWorkbench.preferredPath 改值", workbench("notes"), None, True),
            ("agileWorkbench 整节缺失", lambda manifest: manifest.pop("agileWorkbench"), None, True),
            ("REQUIRED_PROJECT_DIRS 缺 工作台", None, [d for d in real_dirs if d != "工作台"], True),
            ("REQUIRED_PROJECT_DIRS 缺 正文", None, [d for d in real_dirs if d != "正文"], True),
        ]
        for label, mutate, dirs, should_raise in cases:
            with self.subTest(case=label):
                manifest = deepcopy(MANIFEST)
                if mutate is not None:
                    mutate(manifest)
                with patch.object(project, "toolbox_manifest", return_value=manifest), \
                        patch.object(importer, "REQUIRED_PROJECT_DIRS", dirs if dirs is not None else real_dirs):
                    if should_raise:
                        with self.assertRaises(ValueError):
                            check_layout_invariants()
                    else:
                        check_layout_invariants()


if __name__ == "__main__":
    unittest.main()
