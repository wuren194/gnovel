"""Authoritative reads must not serve truth while a transaction is mid-flight."""
from __future__ import annotations

from pathlib import Path

from .structure_store import StructureError


def require_no_pending(root: Path) -> None:
    """Mid-transaction bytes are a mixed version; authoritative queries refuse them.

    调用方可以是已持有项目锁的事务自身（当前真实持锁线程），那正是唯一
    允许看见中间状态的位置。
    """
    root = root.expanduser().resolve()
    pending = root / ".gnovel" / "transactions"
    if not pending.is_dir() or not any(pending.glob("*.json")):
        return
    from .locks import current_thread_owns_lock
    mine = current_thread_owns_lock(root)
    if mine:
        return
    raise StructureError("TRANSACTION_INCOMPLETE",
                         "recover the pending transaction before querying current truth",
                         {"transactions": sorted(path.stem for path in pending.glob("*.json"))})
