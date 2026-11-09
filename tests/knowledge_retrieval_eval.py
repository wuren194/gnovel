"""Fixed retrieval eval for curated knowledge (Post-R1 Audit Round 2).

Metrics (definitions are part of the contract; keep in sync with
docs/KNOWLEDGE-CONSERVATION.md):

- Recall@k: mean over queries with a non-empty expectedUseful of
  ``any(expected ∩ top-k)`` (hit-based; k=1,3). Queries that allowEmpty are
  excluded from recall denominators.
- Precision@3: mean over queries with non-empty expectedUseful of
  ``|expected ∩ returned| / max(1, |returned|)``. A query that returns nothing
  scores 0.
- MRR: mean over queries with non-empty expectedUseful of ``1 / rank`` of the
  first expected item (0 when absent).
- No-result Accuracy: fraction of allowEmpty queries that return ``items == []``.
- Harmful Retrieval Rate: fraction of ALL queries whose returned items
  intersect mustNotReturn (query-level). Reported separately from
  harmfulItemRatio (item-level must-not count / returned count); the two are
  never merged.

The eval reads the real toolbox corpus (knowledge/assets, repository-owned
material) and is fully deterministic: same corpus + registry + index produce
the same ranking, items and excerpts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / 'scripts')):  # both `-m tests.x` and direct runs
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'knowledge-retrieval-eval.json'
STATIONS = {'composer', 'router', 'planner', 'writer', 'editor', 'titler'}
# tuning/holdout are writing-vocabulary probes; real holds anonymized chapter goals
# taken from local projects, so plot-language recall is measured, not assumed.
SPLITS = ('tuning', 'holdout', 'real')


def load_fixture() -> dict:
    payload = json.loads(FIXTURE.read_text(encoding='utf-8'))
    cases = payload['cases']
    validate_fixture(cases)
    return payload


def validate_fixture(cases: list[dict]) -> None:
    from core.knowledge_store import list_assets
    known = {row['assetId']: row for row in list_assets()['items']}
    seen = set()
    for case in cases:
        cid = case['id']
        assert cid not in seen, f'duplicate eval id: {cid}'
        seen.add(cid)
        assert case['station'] in STATIONS, f'{cid}: invalid station'
        assert case['split'] in SPLITS, f'{cid}: invalid split'
        expected, banned = case['expectedUseful'], case['mustNotReturn']
        assert case['allowEmpty'] == (not expected), f'{cid}: allowEmpty must equal empty expectedUseful'
        assert not set(expected) & set(banned), f'{cid}: expected and must-not overlap'
        for asset_id in expected + banned:
            assert asset_id in known, f'{cid}: unknown asset {asset_id}'
        for asset_id in expected:
            row = known[asset_id]
            assert row['status'] == 'active', f'{cid}: expected asset {asset_id} is not active'
            assert case['station'] in row['stations'], \
                f'{cid}: expected asset {asset_id} is not available at station {case["station"]}'
        if case.get('excerptMustContain'):
            assert expected, f'{cid}: excerptMustContain requires expectedUseful'
    splits = {case['split'] for case in cases}
    assert splits == set(SPLITS), 'fixture must define every split'


def ensure_index_current() -> dict:
    from core.knowledge_index import index_state, rebuild_index
    from core.knowledge_store import list_assets
    state = index_state(list_assets()['items'])
    if not state['current']:
        rebuild_index()
        state = {'current': True, 'reason': 'rebuilt'}
    return state


def _mean(values) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def compute_metrics(rows: list[dict]) -> dict:
    """Aggregate the fixed-eval metrics from per-case rows (single source of truth
    for every denominator).

    MRR averages ``1 / rank`` over EVERY query with a non-empty expectedUseful; a
    query whose expected item is absent (``rank == 0``) contributes ``0.0`` rather
    than dropping out of the denominator. Recall/Precision/No-result follow the
    same full-denominator rule. With no relevant query, MRR is ``0.0`` (never None).
    """
    relevant_rows = [row for row in rows if row['expected']]
    empty_rows = [row for row in rows if not row['expected']]
    returned_items = sum(len(row['returned']) for row in rows)
    harmful_items = sum(len(set(row['returned']) & set(row['mustNotReturn'])) for row in rows)
    mrr = _mean((1 / row['rank']) if row['rank'] else 0.0 for row in relevant_rows)
    metrics = {
        'cases': len(rows),
        'relevantCases': len(relevant_rows),
        'emptyCases': len(empty_rows),
        'recallAt1': _mean(row['hitAt1'] for row in relevant_rows),
        'recallAt3': _mean(row['hitAt3'] for row in relevant_rows),
        'precisionAt3': _mean(row['precision'] for row in relevant_rows),
        'mrr': 0.0 if mrr is None else mrr,
        'noResultAccuracy': _mean(row['emptyOk'] for row in empty_rows),
        'harmfulRate': _mean(row['harmful'] for row in rows),
        'harmfulItemRatio': (harmful_items / returned_items) if returned_items else None,
        'returnedItems': returned_items,
        'excerptChecked': sum('excerptOk' in row for row in rows),
        'excerptFailures': [row['id'] for row in rows if row.get('excerptOk') is False],
    }
    by_category = {}
    for category in sorted({row['category'] for row in rows}):
        subset = [row for row in rows if row['category'] == category]
        sub_relevant = [row for row in subset if row['expected']]
        by_category[category] = {
            'cases': len(subset),
            'recallAt3': _mean(row['hitAt3'] for row in sub_relevant),
            'harmfulRate': _mean(row['harmful'] for row in subset),
            'noResultAccuracy': _mean(row['emptyOk'] for row in subset if row['emptyOk'] is not None),
        }
    return {'metrics': metrics, 'byCategory': by_category}


def evaluate(*, split: str | None = None, route=None, pack=None) -> dict:
    """Run the fixture; `route`/`pack` are injectable for ablation runs."""
    ensure_index_current()
    from core.knowledge_router import build_knowledge_pack, route_knowledge
    route = route or route_knowledge
    pack = pack or build_knowledge_pack
    fixture = load_fixture()
    cases = [case for case in fixture['cases'] if split is None or case['split'] == split]
    rows = []
    started = time.perf_counter()
    for case in cases:
        result = route(planningIntent=case['query'], station=case['station'])
        ids = [item['assetId'] for item in result['items']]
        expected, banned = set(case['expectedUseful']), set(case['mustNotReturn'])
        relevant = bool(expected)
        rank = next((index + 1 for index, asset_id in enumerate(ids) if asset_id in expected), 0)
        row = {
            'id': case['id'], 'split': case['split'], 'category': case['category'],
            'query': case['query'], 'station': case['station'],
            'returned': ids, 'expected': sorted(expected), 'mustNotReturn': sorted(banned),
            'hitAt1': relevant and rank == 1,
            'hitAt3': relevant and rank != 0,
            'rank': rank if relevant else None,
            'precision': (len(expected & set(ids)) / max(1, len(ids))) if relevant else None,
            'harmful': bool(set(ids) & banned),
            'emptyOk': (ids == []) if not relevant else None,
        }
        if case.get('excerptMustContain'):
            packed = pack(planningIntent=case['query'], station=case['station'])
            item = next((entry for entry in packed['items'] if entry['assetId'] in expected), None)
            excerpt = item['excerpt'] if item else ''
            missing = [needle for needle in case['excerptMustContain'] if needle not in excerpt]
            row['excerptOk'] = item is not None and not missing
            row['excerptMissing'] = missing
            if item is not None:
                row['sectionHeading'] = item.get('sectionHeading')
        rows.append(row)
    total_ms = (time.perf_counter() - started) * 1000

    summary = compute_metrics(rows)
    metrics = summary['metrics']
    metrics['totalMs'] = round(total_ms, 2)
    metrics['avgMs'] = round(total_ms / len(rows), 3) if rows else None
    return {'fixtureSha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            'split': split or 'all', 'metrics': metrics,
            'byCategory': summary['byCategory'], 'rows': rows}


def format_report(report: dict) -> str:
    metrics = report['metrics']
    lines = [f"split={report['split']} fixture={report['fixtureSha256'][:12]}",
             f"Recall@1={metrics['recallAt1']:.3f} Recall@3={metrics['recallAt3']:.3f} "
             f"Precision@3={metrics['precisionAt3']:.3f} MRR={metrics['mrr']:.3f}",
             f"NoResult={metrics['noResultAccuracy']:.3f} HarmfulRate={metrics['harmfulRate']:.3f} "
             f"HarmfulItemRatio={metrics['harmfulItemRatio']:.3f}",
             f"cases={metrics['cases']} totalMs={metrics['totalMs']} avgMs={metrics['avgMs']}",
             f"excerptFailures={metrics['excerptFailures']}"]
    for category, data in sorted(report['byCategory'].items()):
        lines.append(f"  {category:16s} cases={data['cases']:2d} recall@3={data['recallAt3']!s:6s} "
                     f"harmful={data['harmfulRate']!s:6s} no-result={data['noResultAccuracy']!s}")
    fails = [row for row in report['rows']
             if (row['expected'] and not row['hitAt3']) or row['harmful']
             or row['emptyOk'] is False or row.get('excerptOk') is False]
    if fails:
        lines.append('failures:')
        for row in fails:
            lines.append(f"  {row['id']} [{row['category']}] {row['query']!r} -> {row['returned']} "
                         f"expected={row['expected']} banned={row['mustNotReturn']} "
                         f"excerpt={row.get('excerptMissing', '')}")
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Fixed knowledge retrieval eval (Round 2).')
    parser.add_argument('--split', choices=[*SPLITS, 'all'], default='all')
    parser.add_argument('--json', action='store_true', help='print the full report as JSON')
    parser.add_argument('--report-out', default=None, help='write the full report JSON to this path')
    args = parser.parse_args(argv)
    report = evaluate(split=None if args.split == 'all' else args.split)
    if args.report_out:
        Path(args.report_out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding='utf-8')
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
