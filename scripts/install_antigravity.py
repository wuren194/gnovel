#!/usr/bin/env python3
"""Install and verify the govel toolbox for Antigravity.

Antigravity 的定制发现机制（见 `~/.gemini/antigravity/builtin/skills/agy-customizations`）：

- 工作区级：仓库内 `.agents/skills/` + `.agents/skills.json`，优先级最高，随 VCS 走，
  无需本脚本处理。
- 全局级：`~/.gemini/config/skills/<name>/`，机器本地。本脚本维护这一层：把仓库
  顶层需要的项目软链进去，让 Antigravity 在任意工作区都能加载完整工具箱。

为什么必须显式维护软链清单：`toolbox.manifest.json` 曾整体缺链——它是 promptPlan
的唯一真源，六个工位 SKILL 都声明「契约装载以 manifest 为准」，缺它等于工位不知道
该加载哪些契约。`corpus/` 也曾只链了 catalog.json 而漏掉 legacy/，导致 catalog 里
252 条模块路径全部不可达，席3 的强制取材必然失败。

用法：

    python3 scripts/install_antigravity.py            # 幂等安装/修复
    python3 scripts/install_antigravity.py --check    # 只报告，不改动
    python3 scripts/install_antigravity.py --json     # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from scripts.core.antigravity_registration import RegistrationError, inspect_entry, install_registration, pending_registration, read_configuration
except ImportError:
    from core.antigravity_registration import RegistrationError, inspect_entry, install_registration, pending_registration, read_configuration

REPO = Path(__file__).resolve().parent.parent
GEMINI = Path.home() / ".gemini"
# 官方文档声明的全局定制位置；antigravity/skills 是它的同步视图。
SKILL_ROOT = GEMINI / "config" / "skills" / "govel"
MCP_CONFIG = GEMINI / "config" / "mcp_config.json"

def _skill_links() -> tuple[dict[str, str], str | None]:
    """链接清单与读取失败原因。

    manifest 不可读时仍返回基础条目（SKILL.md、shared、scripts 等），同时给出原因——
    此前直接返回空清单，`check()` 遍历空集合报「全部就位」，安装器在真正需要它的
    时刻（仓库半损）反而 fail-open（机制 39 修复）。
    """
    links = {
        "SKILL.md": "总入口：工位表、项目布局、规则优先级、核心命令",
        "README.md": "快速开始与诊断命令",
        "AGENTS.md": "工具箱仓库规则（Antigravity 按目录层级自动加载）",
        "toolbox.manifest.json": "promptPlan 唯一真源；缺它工位不知道装载哪些契约",
        "shared": "契约与注册表",
        "scripts": "运行时：novelctl / aggregate_diagnostics / mcp_server",
        "knowledge": "数据接入规范与原创示例；用户资料在仓库外",
        "docs": "架构、CHANGELOG、契约沿革",
    }
    try:
        manifest = json.loads((REPO / "toolbox.manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return links, f"toolbox.manifest.json 不可读（{exc}）：工位条目无法枚举，安装/校验不完整"
    for skill in manifest.get("skills", []):
        links[skill] = "工位真源"
    return links, None


LINKS, MANIFEST_ERROR = _skill_links()

# 已作废、不得留在 skills 目录的条目。
OBSOLETE = {
    "govel-roundtable": "自称替代 G-MULTI-IDENTITY-REVIEW v2.3.0，且依赖 GEMINI.md 全局规则；现行契约为 v2.6.0，双真源风险",
    "govel-setup": "目录已删除，agents-template.md 迁至 shared/templates/",
}

MCP_SERVER = REPO / "scripts" / "mcp_server.py"


def _mcp_status() -> dict:
    report = {'configPath': str(MCP_CONFIG), 'configHealthy': False,
              'connectionVerified': False, 'verification': 'configuration_only',
              'hostConnection': 'not_observed', 'problems': []}
    try:
        pending = pending_registration(MCP_CONFIG)
        if pending is not None:
            raise RegistrationError('MCP_INSTALL_INCOMPLETE', '存在未完成的 MCP 安装；重新运行安装器先恢复实际备份',
                                    backup_path=pending['backupPath'])
        snapshot = read_configuration(MCP_CONFIG)
        if snapshot['raw'] is None:
            report['problems'] = [{'code': 'MCP_CONFIG_MISSING', 'message': '官方全局 MCP 配置文件不存在'}]
        else:
            entry = snapshot['data'].get('mcpServers', {}).get("govel")
            report.update(inspect_entry(entry, MCP_SERVER))
    except (RegistrationError, OSError) as exc:
        report['problems'] = [{'code': getattr(exc, 'code', 'MCP_CONFIG_UNREADABLE'),
                               'message': str(exc) if isinstance(exc, RegistrationError) else 'MCP 配置无法读取'}]
        if getattr(exc, 'backup_path', None):
            report['backupPath'] = exc.backup_path
    report['status'] = 'configured' if report['configHealthy'] else 'needs_repair'
    return report


def check() -> dict:
    """只读检查，返回结构化报告。"""
    problems: list[str] = []
    fixes: list[str] = []
    if MANIFEST_ERROR:
        problems.append(MANIFEST_ERROR)

    mcp = _mcp_status()
    problems.extend('MCP：' + row['message'] for row in mcp['problems'])
    if not SKILL_ROOT.parent.is_dir():
        problems.append(f"Antigravity 全局定制目录不存在：{SKILL_ROOT.parent}（未安装 Antigravity？）")
        return {"ok": False, "problems": problems, "fixes": fixes, "installed": False,
                "skillRoot": str(SKILL_ROOT), "mcp": mcp}

    # 1. 软链完整性
    for name in LINKS:
        target = REPO / name
        link = SKILL_ROOT / name
        if not target.exists():
            problems.append(f"仓库缺少 {name}（清单与仓库结构不一致）")
            continue
        if not link.exists() and not link.is_symlink():
            fixes.append(f"缺链：{name}")
        elif link.is_symlink():
            resolved = Path(os.readlink(link))
            if not resolved.exists():
                fixes.append(f"断链：{name} -> {resolved}")
            elif resolved.resolve() != target.resolve():
                fixes.append(f"指向错误：{name} -> {resolved}")
        else:
            fixes.append(f"是实体而非软链（内容会与仓库分叉）：{name}")

    # 2. 作废条目残留
    for name, why in OBSOLETE.items():
        if (SKILL_ROOT.parent / name).exists() or (SKILL_ROOT / name).exists():
            fixes.append(f"作废条目残留：{name}（{why}）")

    # 4. 工作区级适配（随仓库走，只报告不修）
    for rel in (".agents/skills.json", ".agents/skills/govel/SKILL.md"):
        if not (REPO / rel).is_file():
            problems.append(f"仓库缺少工作区适配文件：{rel}")

    return {
        "ok": not problems and not fixes,
        "installed": SKILL_ROOT.is_dir(),
        "skillRoot": str(SKILL_ROOT),
        "problems": problems,
        "fixes": fixes,
        "mcp": mcp,
    }


def install(*, force: bool = False) -> dict:
    """幂等安装：重建软链、清除作废条目、修正 MCP 路径。"""
    import shutil
    import time

    actions: list[str] = []
    problems: list[str] = []
    if MANIFEST_ERROR:
        problems.append(MANIFEST_ERROR)

    if not SKILL_ROOT.parent.is_dir():
        return {
            "ok": False,
            "actions": actions,
            "problems": [f"Antigravity 全局定制目录不存在：{SKILL_ROOT.parent}"],
        }

    SKILL_ROOT.mkdir(parents=True, exist_ok=True)

    # 清除作废条目（两个可能位置都清）
    for name in OBSOLETE:
        for parent in (SKILL_ROOT.parent, SKILL_ROOT):
            victim = parent / name
            if victim.is_symlink() or victim.exists():
                if victim.is_dir() and not victim.is_symlink():
                    if not force:
                        problems.append(
                            f"安全阻断：作废项包含真实实体目录而非软链：{victim}。若确认清理，请指定 --force"
                        )
                        continue
                    trash_dir = SKILL_ROOT.parent / f".trash-{int(time.time())}"
                    trash_dir.mkdir(parents=True, exist_ok=True)
                    backup = trash_dir / f"obsolete-{victim.name}"
                    shutil.move(victim, backup)
                    actions.append(f"安全移入备份：{victim} -> {backup}")
                else:
                    victim.unlink()
                    actions.append(f"removed obsolete: {victim}")

    # 重建软链。已经正确的链接必须跳过：每次都 unlink+symlink 虽然最终状态相同，
    # 却让「第二次安装」在动作日志里与「修好一次漂移」完全无法区分——安装器自己的
    # 输出就成了噪声，也无法证明它真的幂等。
    for name in LINKS:
        target = REPO / name
        if not target.exists():
            problems.append(f"仓库缺少 {name}，跳过")
            continue
        link = SKILL_ROOT / name
        if link.is_symlink():
            resolved = Path(os.readlink(link))
            # 目录软链的 is_dir() 会跟随链接返回 True，所以先按 is_symlink() 分流，
            # 再用 readlink 的原样路径比对，避免把已正确的目录链也重建一遍。
            if resolved == target and resolved.exists():
                actions.append(f"already linked: {name}")
                continue
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                if not force:
                    problems.append(
                        f"安全阻断：目标位置存在真实实体目录而非软链：{link}。若确认覆盖，请指定 --force"
                    )
                    continue
                trash_dir = SKILL_ROOT.parent / f".trash-{int(time.time())}"
                trash_dir.mkdir(parents=True, exist_ok=True)
                backup = trash_dir / f"link-{link.name}"
                shutil.move(link, backup)
                actions.append(f"安全移入备份：{link} -> {backup}")
            else:
                link.unlink()
        try:
            link.symlink_to(target)
            actions.append(f"linked: {name}")
        except OSError as exc:
            problems.append(f"创建软链失败 {link} -> {target}: {exc}")

    registration = {'changed': False, 'backupPath': None}
    try:
        registration = install_registration(MCP_CONFIG, MCP_SERVER)
        if registration['changed']:
            actions.append(f"registered MCP govel: {MCP_CONFIG}")
    except (RegistrationError, OSError, ValueError) as exc:
        problems.append('MCP：' + (str(exc) if isinstance(exc, RegistrationError) else '注册保存失败，原配置不被作为成功接入证据'))
        registration['errorCode'] = getattr(exc, 'code', 'MCP_REGISTRATION_FAILED')
        if getattr(exc, 'backup_path', None):
            registration['backupPath'] = exc.backup_path
    mcp = {**_mcp_status(), **registration}
    if not mcp['configHealthy']:
        problems.extend('MCP：' + row['message'] for row in mcp['problems'])
    return {"ok": not problems, "actions": actions, "problems": list(dict.fromkeys(problems)), "mcp": mcp}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the toolbox for Antigravity")
    parser.add_argument("--check", action="store_true", help="只报告，不改动")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--force", action="store_true", help="允许覆盖/迁移非软链的同名实体目录")
    args = parser.parse_args(argv)

    report = check() if args.check else install(force=args.force)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1

    if args.check:
        print(f"Antigravity skill 根：{report.get('skillRoot')}")
        print(f"Antigravity MCP 配置：{report.get('mcp', {}).get('configPath')}")
        if report["ok"]:
            print("✓ 软链与 MCP 静态配置就位；尚未验证运行中的 Antigravity 连接")
        for item in report.get("fixes", []):
            print(f"需修复  {item}")
        for item in report.get("problems", []):
            print(f"问题    {item}")
        if report.get("fixes") and not report.get("problems"):
            print("\n运行 python3 scripts/install_antigravity.py 修复")
    else:
        for item in report["actions"]:
            print(f"  {item}")
        for item in report["problems"]:
            print(f"问题  {item}")
        print("✓ 安装完成" if report["ok"] else "✗ 安装未完全成功")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
