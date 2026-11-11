"""The durable undo-authority record: an auxiliary cross-check on a committed undo.

A receipt on its own is a hand-constructable JSON claim; promoting it to authority is exactly the
Blocker A forgery (a shape-valid receipt with no committed undo excludes a real hop). Every committed
schema-4 undo therefore persists ONE undo authority under ``.govel/undo-operations/``, inside the
SAME ``apply_write_set`` as the restored structure, the receipt and the formal undo journal. It binds
the formal committed undo operation that actually ran (``undoOperationId``, see
:mod:`scripts.core.undo_journal`), the reversed operation's journal (identity + byte digest +
committed after revision/hash), the pre-undo and restored structure revisions and semantic hashes, the
actor/time, and the receipt (path + byte digest).

It is NOT the root of the undone fact — that is the formal committed undo journal in the shared
operation ledger, which this record merely cross-checks:

    receipt  ->  formal committed undo journal  ->  reversed refactor journal
                     ^ auxiliary authority

An authority that names no such committed undo operation, or names a different one, is broken evidence
rather than a second, weaker authority.

Producer (``structure_transaction.undo``) and consumer (``prose_ownership``) both come through this
module so the accepted shape is declared exactly once; neither hardcodes its own copy of the rule.
The authority pins the receipt's bytes (not the reverse), so the two records never form a circular
digest and an isolated forged receipt has no authority to point back at it.
"""
from __future__ import annotations


from .write_sets import json_bytes

# Relative, canonical (POSIX separators) location of the durable undo authority.
UNDO_OPERATION_DIR = '.govel/undo-operations'

def authority_relative_path(operation_id: str, undo_revision: int) -> str:
    """Where the undo authority for ``operation_id`` undone at ``undo_revision`` lives.

    The stem matches the receipt's, so a receipt and its authority are twins the consumer can
    find by name and require to exist together.
    """
    return f'{UNDO_OPERATION_DIR}/{operation_id}-{undo_revision}.json'


def authority_bytes(*, reversed_operation_id: str, undo_operation_id: str, reversed_journal_digest: str,
                    reversed_after_revision: int, reversed_after_hash: str,
                    pre_undo_revision: int, pre_undo_hash: str,
                    restored_revision: int, restored_hash: str,
                    actor: str, committed_at: str, undone_at: str,
                    receipt_path: str, receipt_digest: str) -> bytes:
    """The committed authority body, in the single accepted schema."""
    return json_bytes({
        'operation': 'undo',
        'undoOperationId': undo_operation_id,
        'reversedOperationId': reversed_operation_id,
        'reversedJournalDigest': reversed_journal_digest,
        'reversedAfterRevision': reversed_after_revision,
        'reversedAfterHash': reversed_after_hash,
        'preUndoRevision': pre_undo_revision,
        'preUndoHash': pre_undo_hash,
        'restoredRevision': restored_revision,
        'restoredHash': restored_hash,
        'actor': actor,
        'committedAt': committed_at,
        'undoneAt': undone_at,
        'receiptPath': receipt_path,
        'receiptDigest': receipt_digest,
    })
