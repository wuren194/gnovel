"""Project-relative path validation shared by all read/write surfaces."""

from __future__ import annotations

from pathlib import Path


class ProjectPathError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def recorded_project_roots(root: Path) -> set[str]:
    """Historical locators of this current project, backed by its move receipt.

    Only for stored references. Write destinations still use safe_project_path.
    """
    import json
    base = root.expanduser().resolve()
    roots = {str(base)}
    receipt_path = safe_project_path(base, '.govel/project-locator.json')
    if not receipt_path.is_file():
        return roots
    try:
        receipt = json.loads(receipt_path.read_bytes())
        marker = json.loads(safe_project_path(base, '.govel/project.json').read_bytes())
        if (receipt.get('schemaVersion') != 1 or receipt.get('kind') != 'project_locator'
                or receipt.get('projectId') != marker.get('projectId') or not marker.get('projectId')
                or receipt.get('currentLocator') != str(base) or not isinstance(receipt.get('history'), list)
                or not receipt['history']):
            return roots
        previous = None
        for row in receipt['history']:
            origin, target = row['rootAtOperation'], row['currentLocator']
            if (any(not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts
                    for value in (origin, target)) or not row.get('operationId')
                    or previous is not None and origin != previous):
                return {str(base)}
            roots.add(origin)
            previous = target
        if previous != str(base) or receipt['rootAtOperation'] != receipt['history'][-1]['rootAtOperation']:
            return {str(base)}
        return roots
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {str(base)}


def stored_root_matches(root: Path, recorded: str) -> bool:
    return (isinstance(recorded, str) and
            (recorded == str(root.expanduser().resolve()) or recorded in recorded_project_roots(root)))


def stored_project_path(root: Path, value: str | Path) -> Path:
    """Resolve a retained pointer into the current tree without editing history."""
    base = root.expanduser().resolve()
    candidate = Path(value).expanduser()
    if candidate.is_absolute() and not candidate.is_relative_to(base):
        for origin in sorted(recorded_project_roots(base), key=len, reverse=True):
            if candidate.is_relative_to(origin):
                candidate = candidate.relative_to(origin)
                break
    return safe_project_path(base, candidate, allow_absolute=True)


def safe_project_path(
    root: Path,
    value: str | Path,
    *,
    allow_absolute: bool = False,
    allow_root: bool = False,
    reject_symlink: bool = True,
) -> Path:
    """Resolve a path and ensure it stays inside ``root``."""
    base = root.expanduser().resolve()
    candidate = Path(value).expanduser()
    if candidate.is_absolute() and not allow_absolute:
        raise ProjectPathError(
            "ABSOLUTE_PATH_FORBIDDEN",
            "project-relative path required",
            {"path": str(value)},
        )
    raw = candidate if candidate.is_absolute() else base / candidate
    if reject_symlink:
        # 先检查解析前的原始路径组成部分。`Path.resolve()` 会吞掉内部 symlink，
        # 只检查 resolved 结果无法发现 `工作台/link -> 工作台/real` 这种路径。
        #
        # 锚点同时准备 resolve 前后的形态：base 是 resolve 形态（真身比较用），而
        # 调用方给的绝对路径可能只与未 resolve 的 root 同形态——macOS 上 /var 与
        # /private/var 是同一目录的两种写法，只按 base 对齐时 relative_to 抛
        # ValueError，逐段检查会整段跳过，内链因此漏检（机制 36 审查的 4a 形态）。
        anchors = [base]
        raw_root = root.expanduser()
        if raw_root != base:
            anchors.append(raw_root)
        for anchor in anchors:
            try:
                parts = raw.relative_to(anchor).parts
            except ValueError:
                continue
            current = anchor
            for part in parts:
                current = current / part
                try:
                    if current.is_symlink():
                        raise ProjectPathError(
                            "SYMLINK_PATH_FORBIDDEN",
                            "symlink paths are not allowed",
                            {"path": str(value)},
                        )
                except OSError as exc:
                    raise ProjectPathError("PATH_UNREADABLE", "cannot inspect project path", {"path": str(value)}) from exc
            break
    resolved = raw.resolve(strict=False)
    if resolved == base and not allow_root:
        raise ProjectPathError("PROJECT_ROOT_NOT_ALLOWED", "project root is not a file path")
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ProjectPathError(
            "PATH_OUTSIDE_PROJECT",
            "path escapes project root",
            {"path": str(value)},
        ) from exc
    return resolved
