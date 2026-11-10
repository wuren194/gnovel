"""Read an operation receipt only with its committed write-set evidence."""
from __future__ import annotations

import json
from pathlib import Path

from .jsonio import read_json
from .structure_store import StructureError, structure_hash
from .write_sets import digest, safe_project_path


def require_committed_operation(root: Path, operation_id: str, *, plan_hash: str | None = None) -> dict:
    from .structure_commands import _operation_paths
    from .source_identity import _operation_identities
    _, journal_path, _ = _operation_paths(root, operation_id)
    relative = journal_path.relative_to(root).as_posix()
    try:
        safe_project_path(root, relative)
        _operation_identities(journal_path, relative=relative)
        payload = journal_path.read_bytes()
        record = json.loads(payload)
    except (StructureError, OSError, ValueError, TypeError) as exc:
        raise StructureError('OPERATION_RECEIPT_INVALID', 'operation receipt is missing or incomplete',
                             {'operationId': operation_id}) from exc
    if plan_hash is not None and record.get('planHash') != plan_hash:
        raise StructureError('PLAN_HASH_MISMATCH', 'receipt identifies a different approved plan')
    if record.get('schemaVersion') != 4 or record.get('operationId') != operation_id:
        raise StructureError('OPERATION_RECEIPT_INVALID', 'invalid committed operation identity')
    directory = safe_project_path(root, '.govel/transactions')
    for terminal in directory.glob('TX-*/completed.json'):
        try:
            terminal = safe_project_path(root, terminal.relative_to(root))
            manifest = read_json(terminal)
            if (manifest.get('phase') != 'committed' or manifest.get('publicationProtocol') != 'preserve-exclusive-v1'
                    or manifest.get('transactionId') != terminal.parent.name):
                continue
            rows = {row['path']: row for row in manifest['files']}
            receipt = rows.get(relative, {}).get('after', {})
            source = rows.get('.govel/structure.json', {}).get('after', {})
            if not receipt.get('exists') or receipt.get('sha256') != digest(payload) or not source.get('exists'):
                continue
            prefix = terminal.parent.relative_to(root) / 'after'
            archived = safe_project_path(root, prefix / relative).read_bytes()
            source_bytes = safe_project_path(root, prefix / '.govel/structure.json').read_bytes()
            after = json.loads(source_bytes)
            if (archived == payload and digest(source_bytes) == source['sha256']
                    and after['revision'] == record['afterRevision'] and structure_hash(after) == record['afterHash']):
                return record
        except (StructureError, OSError, ValueError, KeyError, TypeError):
            continue
    raise StructureError('OPERATION_RECEIPT_INVALID', 'operation has no matching committed transaction evidence',
                         {'operationId': operation_id})
