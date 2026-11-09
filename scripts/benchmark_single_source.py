#!/usr/bin/env python3
"""Reproducible correctness/performance fixtures for the single-source design."""
from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.jsonio import write_json
from scripts.core.source_candidate_scan import scan_prose_candidates
from scripts.core.source_content_proposals import resolve_content_target, preview_content_change
from scripts.core.source_query import query_source
from scripts.core.source_render import export_source
from scripts.core.structure_store import load_structure
from scripts.core.structure_transaction import commit
from tests.fixtures_source import source_fixture


def _now(callable_):
    started = time.perf_counter(); value = callable_(); return value, time.perf_counter() - started


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == 'darwin' else value * 1024)


def _json_size(value) -> int:
    return len((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def _source_fixture(root: Path, target_bytes: int) -> dict:
    source = source_fixture()
    chunk = ('世界树年轮记录着枯荣变化，精灵沿着枝脉迁徙。' * 48)[:1024]
    approximate = len(chunk.encode()) + 260
    current = _json_size(source)
    count = max(1, (target_bytes - current + approximate - 1) // approximate)
    for index in range(count):
        source['fragments'].append({
            'fragmentId': f'SF-SCALE-{index:06d}', 'ownerId': 'C-ALPHA',
            'text': chunk, 'order': 100 + index, 'contentBindings': ['AT-LIFE'],
            'dependsOn': [], 'contentRole': 'fact',
        })
    root.joinpath('.govel').mkdir(parents=True); root.joinpath('工作台').mkdir()
    write_json(root / '.govel/structure.json', source)
    return source


def benchmark_source(megabytes: float) -> dict:
    with tempfile.TemporaryDirectory(prefix='single-source-scale-') as directory:
        root = Path(directory); target = int(megabytes * 1024 * 1024)
        _, generate = _now(lambda: _source_fixture(root, target))
        actual = (root / '.govel/structure.json').stat().st_size
        loaded, cold = _now(lambda: load_structure(root))
        selection = {'ownerIds': ['C-ALPHA']}
        overview, overview_time = _now(lambda: query_source(root, selection=selection, view='overview', limit=20))
        page, list_time = _now(lambda: query_source(root, selection=selection, view='list', limit=20))
        exported, export_time = _now(lambda: export_source(
            root, output='工作台/规模全文设定.md', selection=selection))
        changed = deepcopy(loaded['fragments'][-1]); changed['text'] += '修订。'
        def proposal():
            resolved = resolve_content_target(root, recordId=changed['fragmentId'], kind='fact')
            return preview_content_change(root, sessionRef=resolved['sessionRef'],
                                          expectedOldRecord=resolved['record'], newRecord=changed,
                                          reason='规模夹具单片段修订',
                                          evidence=[{'fragmentId': changed['fragmentId'],
                                                     'quote': resolved['record']['text']}],
                                          approvalActor='benchmark', approvalEvidence='测试夹具')
        plan, preview_time = _now(proposal)
        receipt, commit_time = _now(lambda: commit(root, operation_id=plan['operationId'],
                                                   plan_hash=plan['planHash'], approved=True, actor='benchmark'))
        return {'kind': 'source', 'requestedMiB': megabytes, 'sourceBytes': actual,
                'fragmentCount': len(loaded['fragments']), 'generateSeconds': generate,
                'coldLoadValidateSeconds': cold, 'overviewSeconds': overview_time,
                'firstListPageSeconds': list_time, 'exportSeconds': export_time,
                'exportBytes': exported['bytes'], 'changePreviewSeconds': preview_time,
                'changeCommitSeconds': commit_time, 'revisionAfterCommit': receipt['afterRevision'],
                'queryReturned': page['returned'], 'queryHasMore': page['hasMore'],
                'overviewOwnerCount': overview['totalMatching'], 'peakRssBytes': _rss_bytes(),
                'correctness': {'exportComplete': exported['recordCount'] == page['totalMatching'],
                                'singleFragmentChanged': plan['impact']['changedIds'] == [changed['fragmentId']]}}


def _prose_fixture(root: Path, characters: int) -> tuple[dict, list[str], int]:
    source = source_fixture(); volume_id = source['volumes'][0]['volumeId']; arc_id = source['arcs'][0]['arcId']
    per_chapter = 4000; chapter_count = max(1, (characters + per_chapter - 1) // per_chapter)
    ids, actual = [], 0
    (root / '.govel').mkdir(parents=True); (root / '正文').mkdir()
    base = ('他沿着长廊向前，记住每一扇门的位置。' * 250)[:per_chapter]
    for ordinal in range(1, chapter_count + 1):
        cid = f'CH-SCALE-{ordinal:06d}'; ids.append(cid)
        remaining = max(0, characters - actual); text = base[:min(per_chapter, remaining)]
        if ordinal % 100 == 0 and len(text) >= 4:
            text = '救了' + text[2:]
        actual += len(text)
        source['chapters'].append({'chapterId': cid, 'displayOrdinal': ordinal,
            'displayCode': f'CH{ordinal:04d}', 'title': f'第{ordinal}章',
            'placement': {'scope': 'volume', 'volumeId': volume_id},
            'filePath': f'正文/{cid}.md', 'status': 'stable',
            'storyTimeStartId': None, 'storyTimeEndId': None})
        source['chapterArcLinks'].append({'chapterId': cid, 'arcId': arc_id, 'role': 'primary'})
        (root / f'正文/{cid}.md').write_text(text, encoding='utf-8')
    write_json(root / '.govel/structure.json', source)
    return source, ids, actual


def benchmark_prose(characters: int) -> dict:
    with tempfile.TemporaryDirectory(prefix='single-source-prose-') as directory:
        root = Path(directory)
        (source, ids, actual), generate = _now(lambda: _prose_fixture(root, characters))
        result, scan = _now(lambda: scan_prose_candidates(root, terms=['救了'], chapterIds=ids,
                                                           targetIds=['AT-LIFE'], limit=100))
        path = root / source['chapters'][-1]['filePath']; path.write_text(path.read_text(encoding='utf-8') + '救了', encoding='utf-8')
        changed, changed_scan = _now(lambda: scan_prose_candidates(root, terms=['救了'],
                                                                    chapterIds=[ids[-1]], limit=100))
        expected = len([ordinal for ordinal in range(1, len(ids) + 1) if ordinal % 100 == 0])
        return {'kind': 'prose', 'requestedCharacters': characters, 'actualCharacters': actual,
                'chapterCount': len(ids), 'generateSeconds': generate, 'fullScanSeconds': scan,
                'changedChapterScanSeconds': changed_scan, 'candidateCount': result['totalMatching'],
                'changedChapterCandidateCount': changed['totalMatching'], 'peakRssBytes': _rss_bytes(),
                'correctness': {'allChaptersCovered': result['coverage']['completeForRequestedScope'],
                                'expectedCandidates': result['totalMatching'] == expected,
                                'changedChapterDetected': changed['totalMatching'] >= 1}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='kind', required=True)
    source = sub.add_parser('source'); source.add_argument('--megabytes', type=float, required=True)
    prose = sub.add_parser('prose'); prose.add_argument('--characters', type=int, required=True)
    args = parser.parse_args(argv)
    result = benchmark_source(args.megabytes) if args.kind == 'source' else benchmark_prose(args.characters)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(result['correctness'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
