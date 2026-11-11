"""The one durable undo-receipt schema: naming, writing and validating it.

Every committed undo of a schema-4 operation restores the structure through the shared write set
and, inside the SAME transaction, persists one receipt under ``.govel/undo-receipts/``. This
receipt is the only operation-specific undo evidence that survives: the ``UNDO-`` marker in
``structure.lastOperation`` is overwritten by the next legitimate status advance, so relying on it
alone would let a reversed move quietly re-enter live lineage (and would refuse a chapter the undo
restored to its own anchor).

A receipt is an INDEX, NOT AN AUTHORITY. It names the operation it reverses AND the formal committed
undo operation that actually ran (``undoOperationId``, a fresh ``OP-`` id in the shared ledger, see
:mod:`scripts.core.undo_journal`). Both fields are hand-writable, so neither proves anything on its
own — the consumer establishes an undone fact only when the receipt resolves to that committed undo
journal and, through it, to the reversed operation's journal. A receipt predating the formal undo
operation (no ``undoOperationId``) is not usable evidence and fails closed rather than being
promoted to provenance nothing behind it can corroborate.

Both source modes write it. A single-source undo records its real ``source_impact`` as ``impact``;
a legacy undo has no source model, so ``impact`` is an empty dict — an absence of claims, never a
fabricated one.

Producer (``structure_transaction.undo``) and consumer (``prose_ownership``) both come through this
module so the accepted shape is declared exactly once; neither hardcodes its own copy of the rule.
"""
from __future__ import annotations


from .write_sets import json_bytes

# Relative, canonical (POSIX separators) location of the durable undo evidence.
UNDO_RECEIPT_DIR = '.govel/undo-receipts'

def receipt_relative_path(operation_id: str, undo_revision: int) -> str:
    """Where the receipt for ``operation_id`` undone at ``undo_revision`` lives."""
    return f'{UNDO_RECEIPT_DIR}/{operation_id}-{undo_revision}.json'


def receipt_bytes(*, operation_id: str, undo_operation_id: str, undo_revision: int, actor: str,
                  undone_at: str, impact: dict) -> bytes:
    """The committed receipt body, in the single accepted schema."""
    return json_bytes({'operationId': operation_id, 'undoOperationId': undo_operation_id,
                       'undoRevision': undo_revision, 'actor': actor,
                       'undoneAt': undone_at, 'impact': impact})
