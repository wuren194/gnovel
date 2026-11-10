"""Verify original-byte coverage and current imported prose without caller scripts."""
from pathlib import Path
import json

from .file_preservation import observation
from .manuscript_import import decode_source, digest, plan_hash
from .import_commit import RECEIPT
from .structure_store import StructureError, load_structure
from .write_sets import safe_project_path, capture_guards, validate_guards


def import_receipt(root, source):
    try:
        _, raw = observation(safe_project_path(root, RECEIPT))
        receipt = json.loads(raw or b'')
        plan = receipt['plan']
        if (receipt['projectId'] != source['projectId'] or plan['projectId'] != source['projectId']
                or receipt['approvedImport'] is not True or receipt['planHash'] != plan['planHash']
                or not isinstance(plan['files'], list) or not plan['files']
                or not isinstance(plan['chapters'], list) or not plan['chapters']
                or plan_hash(plan) != plan['planHash'] or len(receipt['chapters']) != len(plan['chapters'])):
            raise ValueError('receipt mismatch')
        seen = set()
        for row, planned in zip(receipt['chapters'], plan['chapters']):
            if ({key: row.get(key) for key in planned} != planned or row['chapterId'] in seen):
                raise ValueError('chapter mapping mismatch')
            seen.add(row['chapterId'])
        return receipt
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise StructureError('IMPORT_RECEIPT_INVALID', 'import receipt or chapter mapping is missing or damaged') from exc


def verify_import(root: Path, *, checkSource=False):
    root = root.expanduser().resolve()
    from .locks import project_lock_reentrant
    with project_lock_reentrant(root):
        return _verify_locked(root, checkSource=checkSource)


def _verify_locked(root, *, checkSource):
    guards = capture_guards(root, ['.govel/structure.json', RECEIPT])
    source = load_structure(root)
    receipt = import_receipt(root, source)
    files, chapters, problems = [], [], []
    originals = {}
    for row in receipt['plan']['files']:
        path = safe_project_path(root, '.govel/import-source/' + row['snapshotPath'])
        state, raw = observation(path)
        guards.append({'path': str(path.relative_to(root)), **{k: state[k] for k in ('exists', 'sha256', 'dev', 'ino')}})
        if raw is None or digest(raw) != row['sourceHash']:
            problems.append({'code': 'IMPORT_SOURCE_COPY_DAMAGED', 'sourcePath': row['sourcePath']})
            continue
        decoded = decode_source(raw, receipt['plan']['encoding']).encode('utf-8')
        originals[row['sourcePath']] = decoded
        record = {'sourcePath': row['sourcePath'], 'originalBytes': len(raw), 'utf8Bytes': len(decoded), 'copyMatches': True}
        if checkSource:
            try:
                _, external = observation(Path(row['absolutePath']))
                record['externalSourceStatus'] = 'missing' if external is None else 'matches' if digest(external) == row['sourceHash'] else 'changed'
            except (OSError, ValueError):
                record['externalSourceStatus'] = 'unreadable'
        files.append(record)
    current = {r['chapterId']: r for r in source['chapters']}
    coverage = {name: 0 for name in originals}
    from .prose_binding import require_observation, OWNED_CLEAN
    for row in receipt['chapters']:
        cid, name = row['chapterId'], row['sourcePath']
        start, end = row['utf8ByteStart'], row['utf8ByteEnd']
        original = originals.get(name)
        if (original is None or type(start) is not int or type(end) is not int or coverage.get(name) != start
                or not start < end <= len(original) or digest(original[start:end]) != row['contentHash']):
            problems.append({'code': 'IMPORT_COVERAGE_MISMATCH', 'chapterId': cid})
            continue
        coverage[name] = end
        chapter = current.get(cid)
        try:
            if chapter is None:
                raise StructureError('IMPORT_CHAPTER_MISSING', 'imported chapter is absent')
            observed = require_observation(root, source, chapter, (OWNED_CLEAN,), action='import verification')
            prose_guard = capture_guards(root, [chapter['filePath']])[0]
            if 'sha256:' + str(prose_guard['sha256']) != observed['observedContentDigest']:
                raise StructureError('IMPORT_INPUT_CHANGED', 'prose changed during verification')
            guards.append(prose_guard)
            matches = observed['observedContentDigest'] == 'sha256:' + row['contentHash']
            if not matches:
                problems.append({'code': 'IMPORT_PROSE_DIFFERS', 'chapterId': cid, 'message': 'current owned prose differs from imported original'})
            chapters.append({'chapterId': cid, 'originalMatches': matches, 'ownership': observed['state']})
        except (OSError, ValueError) as exc:
            problems.append({'code': getattr(exc, 'code', 'IMPORT_PROSE_UNREADABLE'), 'chapterId': cid, 'message': str(exc)})
    for name, raw in originals.items():
        if coverage[name] != len(raw):
            problems.append({'code': 'IMPORT_COVERAGE_INCOMPLETE', 'sourcePath': name})
    passed = not problems
    validate_guards(root, guards, stage='import-verify-complete')
    result = {'ok': passed, 'code': 'IMPORT_VERIFIED' if passed else 'IMPORT_VERIFICATION_FAILED',
              'projectId': source['projectId'], 'fileCount': len(files), 'chapterCount': len(chapters),
              'allImportedTextPreserved': passed, 'originalCopiesVerified': len(originals) == len(receipt['plan']['files']),
              'semanticReviewProven': False, 'externalSourcesChecked': checkSource,
              'externalSourceChangesAffectFrozenImport': False, 'problems': problems,
              'files': files, 'chapters': chapters}
    from .artifact_pack import bounded_authoring_report
    return bounded_authoring_report(root, result)
