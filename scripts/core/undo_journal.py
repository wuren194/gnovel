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
      -> formal committed UNDO operation   (.gnovel/operations/<undoOperationId>.json)
      -> reversed refactor operation       (.gnovel/operations/<reversedOperationId>.json)

``operation`` is ``undo``, the operation names itself with its own fresh ``OP-`` id (never the
reversed operation's, never a ``UNDO-<id>`` marker), and it binds the reversed operation's journal
by byte digest plus the pre-undo and restored structure hashes:

    beforeHash == reversed.afterHash        (it ran while the reversed move was still in effect)
    afterHash  == reversed.beforeHash       (it restored the state the reversed move started from)
    afterRevision == beforeRevision + 1

``structure_hash`` ignores ``revision`` / ``updatedAt`` / ``lastOperation``, so those two identities
are exact. ``beforeHash`` is not self-declared: the producer also persists the real pre-undo
before-image under ``.gnovel/backups/<undoOperationId>/``, so the consumer recomputes it. An
undo is not reversible (``reversible`` is False): this toolbox has no undo-of-undo, and the ledger's
undo path refuses it rather than silently offering one.

Producer (``structure_transaction.undo``) and consumer (``prose_ownership``) both come through this
module so the accepted shape is declared exactly once. What this establishes is formal repository
operation provenance under the project's normal evidence-integrity model — not a signature against
an administrator who may rewrite every journal, backup and hash on the machine.
"""
from __future__ import annotations

import re
from typing import Optional

from .write_sets import json_bytes

# Formal undo operations live in the shared ledger; this is the one place that names the shape.
OPERATIONS_DIR = '.gnovel/operations'
BACKUPS_DIR = '.gnovel/backups'

OPERATION_ID_PATTERN = re.compile(r'OP-[0-9A-F]{16}')
_HEX64 = re.compile(r'[0-9a-f]{64}')

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


def undo_journal_identity(record: object, *, entry_stem: str) -> Optional[tuple[str, int, int]]:
    """``(reversedOperationId, beforeRevision, afterRevision)`` of a formal undo journal, else None.

    Shape and self-naming only: the reversed-journal digest, the hash/revision relationships and
    the receipt cross-binding are bound by the consumer, which is where those files are read.
    ``None`` means this is not usable formal undo provenance — the caller decides broken vs
    non-claim.
    """
    if not isinstance(record, dict) or record.get('operation') != UNDO_OPERATION_NAME:
        return None
    if record.get('schemaVersion') != 4:
        return None
    operation_id = record.get('operationId')
    if not isinstance(operation_id, str) or not OPERATION_ID_PATTERN.fullmatch(operation_id):
        return None
    if operation_id != entry_stem:
        return None
    reversed_operation_id = record.get('reversedOperationId')
    if not isinstance(reversed_operation_id, str) or not OPERATION_ID_PATTERN.fullmatch(reversed_operation_id):
        return None
    if reversed_operation_id == operation_id:
        return None  # an undo is its own operation; it never reverses itself
    if record.get('reversible') is not False:
        return None
    before_revision, after_revision = record.get('beforeRevision'), record.get('afterRevision')
    for value in (before_revision, after_revision):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    if after_revision != before_revision + 1:
        return None
    for key in ('beforeHash', 'afterHash', 'reversedJournalDigest', 'receiptDigest'):
        value = record.get(key)
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            return None
    for key in ('actor', 'committedAt', 'receiptPath'):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    if record.get('backupPath') != undo_backup_relative_path(operation_id):
        return None
    if not isinstance(record.get('impact'), dict):
        return None
    return reversed_operation_id, before_revision, after_revision
