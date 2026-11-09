"""Chapter-scoped prose citations, bound to exact file bytes and character spans."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .jsonio import read_json
from .locks import project_lock_reentrant
from .source_model import SourceIndex, snapshot_hash
from .structure_store import StructureError
from .write_sets import apply_write_set, digest, json_bytes, safe_project_path

EVIDENCE_PATH = '.govel/source-prose-evidence.json'
KINDS = {'mentions', 'depends_on'}


def read_evidence(root: Path) -> dict:
    path = safe_project_path(root, EVIDENCE_PATH)
    state = read_json(path) if path.is_file() else {'batches': {}}
    if not isinstance(state, dict) or not isinstance(state.get('batches'), dict):
        raise StructureError('SOURCE_EVIDENCE_INVALID', 'prose evidence ledger is unreadable')
    for key, batch in state['batches'].items():
        if (not isinstance(batch, dict) or batch.get('batchId') != key or
                any(not isinstance(batch.get(k), str) for k in ('chapterId', 'path', 'fileHash', 'sourceHash', 'requestHash', 'actor')) or
                not isinstance(batch.get('citations'), list) or not batch['citations']):
            raise StructureError('SOURCE_EVIDENCE_INVALID', 'invalid evidence batch')
        safe_project_path(root, batch['path'])
        for row in batch['citations']:
            if (not isinstance(row, dict) or
                    any(not isinstance(row.get(k), str) for k in ('evidenceId', 'text', 'kind', 'reason')) or
                    row['kind'] not in KINDS or type(row.get('start')) is not int or type(row.get('end')) is not int or
                    not 0 <= row['start'] < row['end'] or not isinstance(row.get('targetIds'), list) or not row['targetIds'] or
                    any(not isinstance(t, str) for t in row['targetIds']) or not isinstance(row.get('targetHashes'), dict) or
                    set(row['targetHashes']) != set(row['targetIds']) or any(not isinstance(h, str) for h in row['targetHashes'].values())):
                raise StructureError('SOURCE_EVIDENCE_INVALID', 'invalid prose citation')
    return state


def _manuscript(root: Path, index: SourceIndex, chapter_id: str):
    if not isinstance(chapter_id, str) or index.owner_types.get(chapter_id) != 'chapter':
        raise StructureError('SOURCE_EVIDENCE_CHAPTER_INVALID', 'prose evidence requires an existing chapter ID')
    raw = index.objects[chapter_id]['filePath']
    path = safe_project_path(root, raw)
    if not path.is_file():
        raise StructureError('SOURCE_EVIDENCE_FILE_MISSING', 'chapter manuscript is missing', {'chapterId': chapter_id})
    content = path.read_bytes()
    try:
        text = content.decode('utf-8')
    except UnicodeError as exc:
        raise StructureError('SOURCE_EVIDENCE_ENCODING', 'chapter manuscript must be UTF-8') from exc
    return str(path.relative_to(root.resolve())), digest(content), text


def register_evidence(root: Path, *, chapterId: str, fileHash: str, sourceHash: str,
                      requestId: str, citations: list, actor: str = 'app', _input_guards=None) -> dict:
    from .source_review import source_snapshot, fingerprint
    from .source_impact import ReviewInputs
    if not isinstance(requestId, str) or not requestId.strip() or not isinstance(actor, str) or not actor.strip():
        raise StructureError('SOURCE_EVIDENCE_INVALID', 'requestId and actor must be nonempty strings')
    if not isinstance(citations, list) or not citations:
        raise StructureError('SOURCE_EVIDENCE_INVALID', 'citations must be a nonempty list')
    with project_lock_reentrant(root):
        source = source_snapshot(root)
        index = SourceIndex.build(source)
        inputs = ReviewInputs(source)
        state = deepcopy(read_evidence(root))
        batch_id = 'PE-' + fingerprint({'projectId': source['projectId'], 'requestId': requestId}).upper()
        request_hash = fingerprint({'chapterId': chapterId, 'fileHash': fileHash, 'sourceHash': sourceHash,
                                    'citations': citations, 'actor': actor})
        prior = state['batches'].get(batch_id)
        if prior is not None:
            if prior.get('requestHash') != request_hash:
                raise StructureError('SOURCE_REQUEST_REUSED', 'evidence requestId belongs to another payload')
            return {'ok': True, 'code': 'SOURCE_EVIDENCE_ALREADY_RECORDED', 'batchId': batch_id,
                    'alreadyRecorded': True, 'historicalReceipt': True}
        if sourceHash != snapshot_hash(source):
            raise StructureError('SOURCE_EVIDENCE_STALE', 'source changed before dependency registration')
        path, current_hash, text = _manuscript(root, index, chapterId)
        if current_hash != fileHash:
            raise StructureError('SOURCE_EVIDENCE_STALE', 'manuscript changed before citation registration')
        normalized, seen = [], set()
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {'start', 'end', 'text', 'targetIds', 'kind', 'reason'}:
                raise StructureError('SOURCE_EVIDENCE_INVALID', 'citation fields must match the citation schema')
            start, end = citation['start'], citation['end']
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or text[start:end] != citation['text']:
                raise StructureError('SOURCE_EVIDENCE_QUOTE_MISMATCH', 'citation does not match exact manuscript characters')
            targets = citation['targetIds']
            if not isinstance(targets, list) or not targets or any(not isinstance(t, str) or t not in index.records for t in targets):
                raise StructureError('SOURCE_EVIDENCE_TARGET_INVALID', 'citations require existing source targets')
            if not isinstance(citation['kind'], str) or citation['kind'] not in KINDS or not isinstance(citation['reason'], str) or not citation['reason'].strip():
                raise StructureError('SOURCE_EVIDENCE_INVALID', 'typed relationship and reason are required')
            row = {**deepcopy(citation), 'targetIds': sorted(set(targets))}
            identity = fingerprint({'chapterId': chapterId, 'fileHash': fileHash, 'start': start, 'end': end,
                                    'targetIds': row['targetIds'], 'kind': row['kind']})
            if identity in seen:
                raise StructureError('SOURCE_EVIDENCE_DUPLICATE', 'duplicate citation in batch')
            seen.add(identity)
            row['evidenceId'] = 'PX-' + identity.upper()
            row['targetHashes'] = {t: inputs.fingerprint(t) for t in row['targetIds']}
            normalized.append(row)
        state['batches'][batch_id] = {'batchId': batch_id, 'requestHash': request_hash, 'chapterId': chapterId,
                                     'path': path, 'fileHash': fileHash, 'sourceHash': sourceHash,
                                     'actor': actor, 'citations': normalized}
        apply_write_set(root, {EVIDENCE_PATH: json_bytes(state)}, operation='source_prose_evidence', guards=_input_guards)
        return {'ok': True, 'code': 'SOURCE_EVIDENCE_RECORDED', 'batchId': batch_id,
                'citationCount': len(normalized), 'proseWrite': False,
                'coverage': {'wholeChapterReviewed': False, 'semanticsAssertedBy': actor}}


def evidence_rows(root: Path, source: dict) -> tuple[list, dict]:
    """Re-check live bytes; never upgrade keyword mentions to dependencies."""
    from .source_review import fingerprint
    from .source_impact import ReviewInputs
    index, state = SourceIndex.build(source), read_evidence(root)
    inputs = ReviewInputs(source)
    manuscripts, rows = {}, {}
    for batch in state['batches'].values():
        cid = batch['chapterId']
        if cid not in manuscripts:
            if index.owner_types.get(cid) != 'chapter':
                manuscripts[cid] = (None, None, '')
            else:
                try:
                    manuscripts[cid] = _manuscript(root, index, cid)
                except StructureError as exc:
                    if exc.code != 'SOURCE_EVIDENCE_FILE_MISSING':
                        raise
                    manuscripts[cid] = (None, None, '')
        path, current_hash, _ = manuscripts[cid]
        current = current_hash == batch['fileHash'] and path == batch['path']
        for citation in batch['citations']:
            missing = [rid for rid in citation['targetIds'] if rid not in index.records]
            changed = [rid for rid in citation['targetIds']
                       if inputs.fingerprint(rid) != citation['targetHashes'][rid]]
            rows[citation['evidenceId']] = {**citation, 'chapterId': cid,
                'locator': {'path': batch['path'], 'fileHash': batch['fileHash'], 'start': citation['start'], 'end': citation['end']},
                'locatorCurrent': current, 'missingTargetIds': missing, 'changedTargetIds': changed,
                'status': 'stale_location' if not current else 'missing_target' if missing else 'needs_review' if changed else 'registered'}
    return sorted(rows.values(), key=lambda r: r['evidenceId']), {
        'ledgerHash': fingerprint(state), 'manuscriptStateHash': fingerprint({cid: data[1] for cid, data in sorted(manuscripts.items())})}


def query_evidence(root: Path, *, chapterId=None, targetId=None, limit=None, maxBytes=None, cursor=None) -> dict:
    from .source_review import source_snapshot, bounded_page
    source = source_snapshot(root)
    rows, snapshot = evidence_rows(root, source)
    index = SourceIndex.build(source)
    if chapterId is not None and (not isinstance(chapterId, str) or index.owner_types.get(chapterId) != 'chapter'):
        raise StructureError('SOURCE_EVIDENCE_CHAPTER_INVALID', 'unknown chapter')
    if targetId is not None and not isinstance(targetId, str):
        raise StructureError('SOURCE_EVIDENCE_TARGET_INVALID', 'targetId must be a string')
    rows = [r for r in rows if (chapterId is None or r['chapterId'] == chapterId) and
            (targetId is None or targetId in r['targetIds'])]
    return bounded_page(rows, snapshot={**snapshot, 'sourceHash': snapshot_hash(source)},
                        selection={'chapterId': chapterId, 'targetId': targetId}, code='SOURCE_PROSE_EVIDENCE',
                        limit=limit, maxBytes=maxBytes, cursor=cursor,
                        coverage={'wholeManuscriptsReviewed': False, 'scope': 'registered citations only'})


def prose_impact(root: Path, source: dict, affected_ids: set) -> tuple[list, list]:
    rows, _ = evidence_rows(root, source)
    matches, chapters = [], set()
    for row in rows:
        matched = affected_ids.intersection(row['targetIds'])
        if not matched:
            continue
        matches.append({'evidenceId': row['evidenceId'], 'chapterId': row['chapterId'],
                        'matchedTargetIds': sorted(matched), 'kind': row['kind'],
                        'locator': row['locator'], 'locatorCurrent': row['locatorCurrent']})
        if row['locatorCurrent'] and row['kind'] == 'depends_on':
            chapters.add(row['chapterId'])
    return matches, sorted(chapters)
