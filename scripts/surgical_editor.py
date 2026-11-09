#!/usr/bin/env python3
"""
Surgical Editor CLI for gnovel Engine.
Allows extracting precise paragraph context slices (with surrounding buffers) and performing targeted line-range edits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from core.exit_codes import EXIT_FAIL_CLOSED, FailClosedArgumentParser
    from core.locks import project_lock
    from core.project import protected_paths
    from core.structure_store import StructureError
except ImportError:  # 作为 scripts 包模块被导入（如 tests）时走包路径
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, FailClosedArgumentParser
    from scripts.core.locks import project_lock
    from scripts.core.project import protected_paths
    from scripts.core.structure_store import StructureError


def _validate_line_range(total: int, start_line: int, end_line: int) -> None:
    if start_line < 1 or end_line > total or start_line > end_line:
        raise ValueError(f"行号范围 {start_line}-{end_line} 无效：文件共 {total} 行。")


def _read_raw(file_path: Path) -> str:
    """以 newline="" 读取，保留原始行尾（CRLF 的 \r 不被 universal newline 吃掉）。

    默认文本模式会把 \r\n 翻译成 \n，导致无法检测/保留 CRLF。
    """
    with open(file_path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _read_lines(raw: str) -> list[str]:
    """按 \n 切行，并把每行行尾的 \r 剥掉（CRLF 文件的 \r 不再留在行内）。

    刻意不用 str.splitlines()：后者会在 U+2028/U+000B/U+000C 上额外切行，
    与 grep -n / 编辑器 / Read 的行号分道扬镳，导致行号错位补丁。
    末尾换行不产生多余空行。写回时由 _write_lines 按检测到的换行风格重连。
    """
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _replacement_lines(new_text: str) -> list[str]:
    """把替换文本切成行（剥掉可能的 \r），与 _read_lines 的行语义一致。"""
    lines = new_text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _write_lines(file_path: Path, lines: list[str], newline: str, trailing: bool) -> None:
    content = newline.join(lines)
    if trailing:
        content += newline
    file_path.write_text(content, encoding="utf-8", newline="")


def extract_surgical_context(file_path: Path, start_line: int, end_line: int, buffer_lines: int = 5) -> str:
    if not file_path.exists():
        raise FileNotFoundError(f"找不到目标文件：{file_path}")

    lines = _read_lines(_read_raw(file_path))
    total = len(lines)

    _validate_line_range(total, start_line, end_line)

    buf_start = max(1, start_line - buffer_lines)
    buf_end = min(total, end_line + buffer_lines)

    prefix_lines = lines[buf_start - 1 : start_line - 1]
    target_lines = lines[start_line - 1 : end_line]
    suffix_lines = lines[end_line : buf_end]

    output = []
    output.append(f"=== SURGICAL EDIT CONTEXT PACK: {file_path.name} (Lines {start_line}-{end_line}) ===")
    if prefix_lines:
        output.append(f"--- [BEFORE BUFFER (Lines {buf_start}-{start_line - 1})] ---")
        output.extend(prefix_lines)
    output.append(f"--- >>> TARGET SURGICAL ZONE (Lines {start_line}-{end_line}) <<< ---")
    output.extend(target_lines)
    if suffix_lines:
        output.append(f"--- [AFTER BUFFER (Lines {end_line + 1}-{buf_end})] ---")
        output.extend(suffix_lines)
    output.append("=== END SURGICAL CONTEXT PACK ===")

    return "\n".join(output)

def apply_surgical_replacement(file_path: Path, start_line: int, end_line: int, new_text: str, allow_delete: bool = False) -> None:
    if not file_path.exists():
        raise FileNotFoundError(f"找不到目标文件：{file_path}")

    raw = _read_raw(file_path)
    newline = "\r\n" if "\r\n" in raw else "\n"
    trailing = raw.endswith("\n")
    lines = _read_lines(raw)
    total = len(lines)

    _validate_line_range(total, start_line, end_line)

    new_lines = _replacement_lines(new_text)
    if not new_lines and not allow_delete:
        raise ValueError(
            "替换文本为空——若意图删除该区间请使用 --delete 标记（防止误删整段正文）"
        )

    updated_lines = lines[: start_line - 1] + new_lines + lines[end_line:]

    # 写回前断言：读取后文件未被并发改动，避免覆盖他人写入
    if _read_raw(file_path) != raw:
        raise ValueError("文件在读取后被修改，已中止写回（请重试）")

    _write_lines(file_path, updated_lines, newline, trailing)
    print(f"[SURGICAL_EDITOR] Successfully replaced lines {start_line}-{end_line} in {file_path.name} ({len(lines)} -> {len(updated_lines)} lines)")

def _project_root_for(probe: Path) -> tuple[Path | None, str | None]:
    """定位项目根，并区分「项目外」与「项目标记缺失」。

    返回与 `probe` 同形态（不 resolve）的项目根，供词法判定使用；resolve 会吞掉
    符号链接段，让「正文 -> 项目外」这类路径看起来仍指向项目内。

    - 找到 `.gnovel/project.json` → `(root, None)`；
    - 祖先里有 `.gnovel/` 但没有 project.json → `(None, 原因)`：项目标记不完整
      时无法确认写入边界，拒绝无锁写入（fail-closed）；
    - 没有任何项目痕迹 → `(None, None)`：项目外文件，无锁写入是既有支持的场景。
    """
    for candidate in (probe.parent, *probe.parent.parents):
        marker_dir = candidate / ".gnovel"
        if (marker_dir / "project.json").is_file():
            return candidate, None
        if marker_dir.is_dir():
            return None, (
                f"{marker_dir} 存在但缺少 project.json——项目标记不完整，"
                "拒绝在无法确认写入边界时修改文件（请恢复项目标记或改用项目外路径）"
            )
    return None, None


def _is_under_protected_path(root: Path, probe: Path) -> bool:
    """目标是否落在受保护路径（正文/、设定/故事档案.md）之下。

    root 与 probe 是同形态的词法路径（见 `_project_root_for`），按相对段判定。
    不经 `safe_project_path`：后者逐段符号链接检查在含符号链接祖先的路径上会整段
    跳过，resolve 之后也判不出「正文 -> 项目外」这类链接段。
    """
    relative = probe.relative_to(root)
    for protected in protected_paths():
        target = Path(protected)
        if relative == target or target in relative.parents:
            return True
    return False


def apply_with_boundary(file_path: Path, start_line: int, end_line: int,
                        new_text: str, *, allow_delete: bool = False) -> None:
    """写受保护路径时在与结构变更相同的项目锁内进行（与最终化串行）。

    人工批准闸仍在 finalize 前（生成器工位持有草稿写权）；这里补的是此前完全
    缺失的互斥——此前 surgical edit 与结构提交并发时会丢彼此的写入。
    边界判定不确定时 fail-closed：项目标记缺失 → 拒绝写入；只有确定在项目外
    才走无锁路径。
    """
    probe = Path(os.path.abspath(file_path.expanduser()))
    root, failure = _project_root_for(probe)
    if failure is not None:
        raise ValueError(failure)
    if root is not None and _is_under_protected_path(root, probe):
        with project_lock(root):
            apply_surgical_replacement(probe, start_line, end_line, new_text, allow_delete=allow_delete)
    else:
        apply_surgical_replacement(probe, start_line, end_line, new_text, allow_delete=allow_delete)


def main() -> int:
    parser = FailClosedArgumentParser(description="gnovel Surgical Editor")
    parser.add_argument("file", type=Path, help="Target markdown file path")
    parser.add_argument("--extract", nargs=2, type=int, metavar=("START", "END"), help="Extract context for line range")
    parser.add_argument("--replace", nargs=2, type=int, metavar=("START", "END"), help="Replace target line range with text from stdin or file")
    parser.add_argument("--text-file", type=Path, help="File containing replacement text for --replace")
    parser.add_argument("--delete", action="store_true", help="Allow empty replacement (delete the line range)")

    args = parser.parse_args()

    try:
        if args.extract:
            start, end = args.extract
            context = extract_surgical_context(args.file, start, end)
            print(context)
            return 0

        if args.replace:
            start, end = args.replace
            if args.text_file:
                replacement_text = args.text_file.read_text(encoding="utf-8")
            else:
                replacement_text = sys.stdin.read()
            apply_with_boundary(args.file, start, end, replacement_text, allow_delete=args.delete)
            return 0
    except (FileNotFoundError, ValueError, StructureError) as exc:
        print(f"❌ 手术编辑失败：{exc}", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    parser.print_help()
    return 1

if __name__ == "__main__":
    sys.exit(main())
