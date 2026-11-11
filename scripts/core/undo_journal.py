"""The formal committed undo operation: the repository-local root of "an undo really happened".

A schema-4 undo receipt and its auxiliary authority are both hand-constructable JSON: every field
of the pair (\"this receipt reverses ``OP-A`` at revision N\", \"the pre-undo hash was ``OP-A``'s
after hash\", \"the restored hash was ``OP-A``'s before hash\") can be computed from a real
``OP-A`` journal that was *never* undone. Verified against each other they prove only that their
author was self-consistent, and the transaction manifest that could have witnessed the real
``apply_write_set`` is deleted by ``write_sets._clear()`` the moment the undo commits. So the pair
alone can never establish that a formal undo transaction ever ran.

The root is instead an ordinary entry in the shared operation ledger, written by the real undo
producer in the same atomic write set as the restored structure:

    receipt / auxiliary authority
      -> formal committed UNDO operation   (.govel/operations/<undoOperationId>.json)
      -> reversed refactor operation       (.govel/operations/<reversedOperationId>.json)

``operation`` is ``undo``, the operation names itself with its own fresh ``OP-`` id (never the
reversed operation's, never a ``UNDO-<id>`` marker), and it binds the reversed operation's journal
by byte digest plus the pre-undo and restored structure hashes:

    beforeHash == reversed.afterHash        (it ran while the reversed move was still in effect)
    afterHash  == reversed.beforeHash       (it restored the state the reversed move started from)
    afterRevision == beforeRevision + 1

``structure_hash`` ignores ``revision`` / ``updatedAt`` / ``lastOperation``, so those two identities
are exact. ``beforeHash`` is not self-declared: the producer also persists the real pre-undo
before-image under ``.govel/backups/<undoOperationId>/``, so the consumer recomputes it. An
undo is not reversible (``reversible`` is False): this toolbox has no undo-of-undo, and the ledger's
undo path refuses it rather than silently offering one.

Producer (``structure_transaction.undo``) and consumer (``prose_ownership``) both come through this
module so the accepted shape is declared exactly once. What this establishes is formal repository
operation provenance under the project's normal evidence-integrity model — not a signature against
an administrator who may rewrite every journal, backup and hash on the machine.
"""
from __future__ import annotations

import re

from .write_sets import json_bytes

# Formal undo operations live in the shared ledger; this is the one place that names the shape.
OPERATIONS_DIR = '.govel/operations'
BACKUPS_DIR = '.govel/backups'

OPERATION_ID_PATTERN = re.compile(r'OP-[0-9A-F]{16}')

#: The ledger operation name of a formal undo. Not a refactor move, so lineage never reads it as a hop.
UNDO_OPERATION_NAME = 'undo'


def undo_journal_relative_path(undo_operation_id: str) -> str:
    """Where the formal undo journal lives — the shared operation ledger, not a third store."""
    return f'{OPERATIONS_DIR}/{undo_operation_id}.json'


def undo_backup_relative_path(undo_operation_id: str) -> str:
    """The undo operation's own durable before-image store, beside every other operation's."""
    return f'{BACKUPS_DIR}/{undo_operation_id}'


def undo_journal_bytes(*, undo_operation_id: str, reversed_operation_id: str,
                       reversed_journal_digest: str, actor: str, before_hash: str, after_hash: str,
                       before_revision: int, after_revision: int, impact: dict,
                       backup_path: str, backup_files: list[str], before_files: dict,
                       after_files: dict, existing_files: list[str], created_files: list[str],
                       receipt_path: str, receipt_digest: str, committed_at: str) -> bytes:
    """The committed formal undo journal, in the ledger's schema-4 operation shape."""
    return json_bytes({
        'schemaVersion': 4,
        'operationId': undo_operation_id,
        'operation': UNDO_OPERATION_NAME,
        'actor': actor,
        'beforeHash': before_hash,
        'afterHash': after_hash,
        'beforeRevision': before_revision,
        'afterRevision': after_revision,
        'impact': impact,
        'backupPath': backup_path,
        'backupFiles': list(backup_files),
        'beforeFiles': before_files,
        'afterFiles': after_files,
        'existingFiles': list(existing_files),
        'createdFiles': list(created_files),
        'writtenIndexes': [],
        'refusedIndexes': [],
        'committedAt': committed_at,
        # No undo-of-undo in this toolbox; the ledger's undo path refuses such a journal outright.
        'reversible': False,
        # The operation this one reverses, pinned by identity and by the exact committed bytes.
        'reversedOperationId': reversed_operation_id,
        'reversedJournalDigest': reversed_journal_digest,
        # The receipt is an index INTO this committed operation, never a claim standing alone.
        'receiptPath': receipt_path,
        'receiptDigest': receipt_digest,
    })
