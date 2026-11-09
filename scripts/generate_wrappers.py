#!/usr/bin/env python3
"""Generate thin client discovery wrappers from the canonical skill manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def source_description(skill: str) -> str:
    source = ROOT / ("SKILL.md" if skill == "gnovel" else Path(skill) / "SKILL.md")
    text = source.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        return "gnovel skill discovery wrapper."
    for line in match.group(1).splitlines():
        if line.startswith("description:"):
            return line.partition(":")[2].strip().strip('"')
    return "gnovel skill discovery wrapper."


def display_name(skill: str) -> str:
    return "gnovel" if skill == "gnovel" else skill.removeprefix("gnovel-").title()


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
        desc = f"{desc} Use when this workstation is the selected gnovel route."
    relative = "../../../SKILL.md" if skill == "gnovel" else f"../../../{skill}/SKILL.md"
    source = "SKILL.md" if skill == "gnovel" else f"{skill}/SKILL.md"
    body = f"""# {display_name(skill)} Loader（Antigravity）

执行前读取唯一业务真源：[{source}]({relative})。

准备入口与 MCP／CLI 选择按根 Skill 执行；任务范围与契约读取按真源执行（`promptPlan.{skill}`）。"""
    return f"---\nname: {skill}\ndescription: >-\n  {desc}\n---\n\n{body}\n"


def claude_wrapper(skill: str) -> str:
    desc = source_description(skill)
    relative = "../SKILL.md" if skill == "gnovel" else f"../../{skill}/SKILL.md"
    return f"---\nname: {skill}\ndescription: {yaml_scalar(desc)}\n---\n\n# {display_name(skill)} Loader\n\n唯一业务 Skill：`{relative}`。\n\n执行前读取该文件；准备入口与 MCP／CLI 选择按根 Skill 执行；任务范围与契约读取按真源执行（`promptPlan.{skill}`）。\n"


def codex_wrapper(skill: str) -> str:
    desc = source_description(skill).replace('"', "\\\"")
    relative = "../SKILL.md" if skill == "gnovel" else f"../../{skill}/SKILL.md"
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
    skills = ["gnovel", *manifest["skills"]]
    expected: dict[Path, str] = {}
    for skill in skills:
        expected[ROOT / ".agents" / "skills" / skill / "SKILL.md"] = agent_wrapper(skill)
        if skill != "gnovel":
            expected[ROOT / ".claude" / "agents" / f"{skill}.md"] = claude_wrapper(skill)
            expected[ROOT / ".codex" / "agents" / f"{skill}.toml"] = codex_wrapper(skill)
    expected[ROOT / ".claude" / "SKILL.md"] = claude_wrapper("gnovel")
    return expected


def check() -> dict:
    drift = []
    for path, expected in expected_files().items():
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            drift.append(str(path.relative_to(ROOT)))
    return {"ok": not drift, "drift": drift}


def write() -> dict:
    written = []
    for path, content in expected_files().items():
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
