#!/usr/bin/env python3
"""Generate client discovery wrappers and plugin declarations from shared sources."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

try:
    from scripts.runtime_requirements import MIN_PYTHON
except ImportError:
    from runtime_requirements import MIN_PYTHON

ROOT = Path(__file__).resolve().parents[1]
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def source_description(skill: str) -> str:
    source = ROOT / ("SKILL.md" if skill == "govel" else Path(skill) / "SKILL.md")
    text = source.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        return "govel skill discovery wrapper."
    for line in match.group(1).splitlines():
        if line.startswith("description:"):
            return line.partition(":")[2].strip().strip('"')
    return "govel skill discovery wrapper."


def display_name(skill: str) -> str:
    return "Govel" if skill == "govel" else skill.removeprefix("govel-").title()


# plain scalar 里会改变 YAML 结构或语义的字符：行首指示符、`: ` / 行尾 `:`、` #` 注释、制表/换行。
_YAML_PLAIN_UNSAFE = re.compile(r"(?:^[\s\-?:,\[\]{}#&*!|>'\"%@`])|(?::(?:\s|$))|(?:\s#)|[\n\t]")


def yaml_scalar(desc: str) -> str:
    """描述作为 YAML plain scalar 安全时原样返回；否则加双引号并转义。

    claude 包装的 frontmatter 此前裸写 `description: {desc}`：描述一旦含 `: `、` #`、行首
    指示符或换行，产出的就是非法 YAML，而 `--check` 还会认为与期望一致。兄弟包装本就稳妥
    （Antigravity 用 `>-` 块标量、Codex 转义引号），这里按需加引号补齐——当前描述都安全，
    输出不变、`--check` 零漂移；未来出现含结构符的描述也不会再产出坏 YAML。
    """
    if _YAML_PLAIN_UNSAFE.search(desc):
        escaped = desc.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\t", " ")
        return f'"{escaped}"'
    return desc


def agent_wrapper(skill: str) -> str:
    desc = source_description(skill)
    if "Use when" not in desc:
        desc = f"{desc} Use when this workstation is the selected govel route."
    relative = "../../../SKILL.md" if skill == "govel" else f"../../../{skill}/SKILL.md"
    source = "SKILL.md" if skill == "govel" else f"{skill}/SKILL.md"
    body = f"""# {display_name(skill)} Loader（Antigravity）

执行前读取唯一业务真源：[{source}]({relative})。

准备入口与 MCP／CLI 选择按根 Skill 执行；任务范围与契约读取按真源执行（`promptPlan.{skill}`）。"""
    return f"---\nname: {skill}\ndescription: >-\n  {desc}\n---\n\n{body}\n"


def claude_wrapper(skill: str) -> str:
    desc = source_description(skill)
    relative = "../SKILL.md" if skill == "govel" else f"../../{skill}/SKILL.md"
    return f"---\nname: {skill}\ndescription: {yaml_scalar(desc)}\n---\n\n# {display_name(skill)} Loader\n\n唯一业务 Skill：`{relative}`。\n\n执行前读取该文件；准备入口与 MCP／CLI 选择按根 Skill 执行；任务范围与契约读取按真源执行（`promptPlan.{skill}`）。\n"


def codex_wrapper(skill: str) -> str:
    desc = source_description(skill).replace('"', "\\\"")
    relative = "../SKILL.md" if skill == "govel" else f"../../{skill}/SKILL.md"
    return (
        f'name = "{skill}"\n'
        f'description = "{desc}"\n'
        'developer_instructions = """\n'
        f"# {display_name(skill)} Loader\n\n"
        f"唯一业务 Skill：`{relative}`。\n\n"
        f"执行前读取该文件；准备入口与 MCP／CLI 选择按根 Skill 执行；任务范围与契约读取按真源执行（`promptPlan.{skill}`）。\n"
        '"""\n'
    )


def expected_files() -> dict[Path, str]:
    manifest = json.loads((ROOT / "toolbox.manifest.json").read_text(encoding="utf-8"))
    skills = ["govel", *manifest["skills"]]
    expected: dict[Path, str] = {}
    for skill in skills:
        expected[ROOT / ".agents" / "skills" / skill / "SKILL.md"] = agent_wrapper(skill)
        if skill != "govel":
            expected[ROOT / ".claude" / "agents" / f"{skill}.md"] = claude_wrapper(skill)
            expected[ROOT / ".codex" / "agents" / f"{skill}.toml"] = codex_wrapper(skill)
    expected[ROOT / ".claude" / "SKILL.md"] = claude_wrapper("govel")
    return expected


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def binding_policy(manifest: dict) -> dict:
    policy = json.loads((ROOT / 'shared/registries/integration.json').read_text(encoding='utf-8'))
    keys = {'schemaVersion', 'repository', 'ref', 'handshakeTimeoutSeconds',
            'downloadTimeoutSeconds', 'downloadMaxBytes', 'archiveMaxBytes', 'archiveMaxFiles'}
    if (not isinstance(policy, dict) or set(policy) != keys or policy.get('schemaVersion') != 1
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', policy.get('repository', ''))
            or not isinstance(policy.get('ref'), str) or not policy['ref'].strip()
            or any(type(policy.get(key)) is not int or policy[key] <= 0
                   for key in keys - {'schemaVersion', 'repository', 'ref'})):
        raise ValueError('invalid Govel integration policy')
    return {**policy, 'pythonMinimum': list(MIN_PYTHON), 'projectSchemaVersion': manifest['schemaVersion']}


def _plugin_server(variable: str, *, portable: bool = False) -> dict:
    server = {'command': 'python3', 'args': [
        variable + '/skills/govel-bind/scripts/bind.py', 'serve', '--host', 'plugin',
        '--engine-root', variable,
    ]}
    return {'type': 'stdio', **server} if portable else server


def expected_plugin_files() -> dict[Path, str]:
    """Runtime plugin files, separate from the workstation-wrapper API."""
    manifest = json.loads((ROOT / 'toolbox.manifest.json').read_text(encoding='utf-8'))
    policy = binding_policy(manifest)
    name, version = manifest['name'], manifest['version']
    repository = 'https://github.com/' + policy['repository']
    author = {'name': policy['repository'].split('/')[0], 'url': repository}
    metadata = {'name': name, 'version': version, 'description': source_description('govel'),
                'author': author, 'homepage': repository, 'repository': repository,
                'license': 'AGPL-3.0-only'}
    portable = {'$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
                **metadata, 'extensions': {'com.openai': {
                    'onboardingSkill': './skills/govel-bind/SKILL.md',
                    'requires_local_executor': True,
                    'interface': {'displayName': 'Govel',
                                  'shortDescription': '通过 AI 对话管理小说、证据与受控写入'},
                }}}
    claude = {**metadata, 'mcpServers': {'govel': _plugin_server('${CLAUDE_PLUGIN_ROOT}')}}
    antigravity = {'$schema': 'https://antigravity.google/schemas/v1/plugin.json',
                   **metadata, 'displayName': 'Govel',
                   'suggestedPrompts': ['检查 Govel 是否已接入当前会话', '用 Govel 查看我的小说进度']}
    marketplace = {'name': name, 'interface': {'displayName': 'Govel'}, 'plugins': [{
        'name': name, 'source': {'source': 'local', 'path': './'},
        'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
        'category': 'Productivity',
    }]}
    claude_marketplace = {'name': name, 'description': metadata['description'], 'version': version,
                         'owner': author, 'plugins': [{'name': name, 'source': './'}]}
    daily = f'''---
name: govel
description: {yaml_scalar(source_description('govel'))}
---

# Govel

读取本插件根目录的唯一业务 Skill：[SKILL.md](../../SKILL.md)。MCP／CLI 选择按根 Skill 执行。
本入口随完整插件分发；独立安装接入入口使用 `govel-bind`，不能只复制本目录代替引擎。
'''
    init = '''---
description: 接入或检查当前宿主中的 Govel 插件；此入口不创建小说项目。
---

# 接入 Govel

读取并执行同一插件中的 [govel-bind](../skills/govel-bind/SKILL.md)。这是首次接入入口，不是小说初始化或作品批准。
'''
    return {
        ROOT / 'plugin.json': _json(portable),
        ROOT / 'mcp.json': _json({'$schema': 'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json',
                                 'mcpServers': {'govel': _plugin_server('${PLUGIN_ROOT}', portable=True)}}),
        ROOT / '.claude-plugin/plugin.json': _json(claude),
        ROOT / '.antigravity-plugin/plugin.json': _json(antigravity),
        ROOT / 'mcp_config.json': _json({'mcpServers': {'govel': _plugin_server('${PLUGIN_ROOT}')}}),
        ROOT / '.agents/plugins/marketplace.json': _json(marketplace),
        ROOT / '.claude-plugin/marketplace.json': _json(claude_marketplace),
        ROOT / 'skills/govel-bind/binding.json': _json(policy),
        ROOT / 'skills/govel/SKILL.md': daily,
        ROOT / 'commands/init.md': init,
    }


def _generated_files() -> dict[Path, str]:
    return {**expected_files(), **expected_plugin_files()}


def check() -> dict:
    drift = []
    for path, expected in _generated_files().items():
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            drift.append(str(path.relative_to(ROOT)))
    return {"ok": not drift, "drift": drift}


def write() -> dict:
    written = []
    for path, content in _generated_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            written.append(str(path.relative_to(ROOT)))
    return {"ok": True, "written": written}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只报告包装漂移，不写文件")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = check() if args.check else write()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result.get("drift", result.get("written", [])):
            print(item)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
