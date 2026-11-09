"""Consistent UTF-8 JSON input and atomic output."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any, *, atomic: bool = True) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    write_text(path, payload, atomic=atomic)


def _new_file_mode(path: Path) -> int:
    """新文件的权限位：`.gnovel/` 下的机器状态保持 0600，其余项目文件按 umask。

    `mkstemp` 一律建 0600，直接 `os.replace` 会让生成的 `正文/`、`设定/`、`工作台/`
    文件比手工创建的同级文件更封闭（0644 → 0600）。机器状态不是交付物，维持 0600；
    作者会读的文件跟随 umask，与 `open()` 建文件的默认一致。
    """
    if ".gnovel" in path.parts:
        return 0o600
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _target_mode(path: Path) -> int:
    """覆盖写盘要用的权限位：已存在的文件保留原模式，新文件取默认模式。"""
    try:
        return path.stat().st_mode & 0o7777
    except OSError:
        return _new_file_mode(path)


def write_text(path: Path, payload: str, *, atomic: bool = True) -> None:
    """Write UTF-8 text atomically when replacing a project asset.

    保留目标文件原有的权限位：`os.replace` 不继承模式，此前每次重建都会把 0644 的
    文件静默变成 `mkstemp` 的 0600。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not atomic:
        path.write_text(payload, encoding="utf-8")
        return
    write_bytes(path, payload.encode("utf-8"))


def write_bytes(path: Path, payload: bytes, *, mode: int | None = None) -> None:
    """Durably replace one file, preserving its permissions by default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = _target_mode(path) if mode is None else mode
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def append_text(path: Path, payload: str) -> None:
    """只追加字节，不重写已有内容。

    回写记录这类证据文件承诺「只追加，不删原文」：`read_text` + `write_text` 会经过
    universal newline 把原 CRLF 变 LF、并重建文件（丢掉权限位），字节意义上并不是
    追加。这里用二进制追加，既有内容与模式都不动。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(payload.encode("utf-8"))
