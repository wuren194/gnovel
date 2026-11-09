"""Determine summary rereading inputs; never generate summary prose."""
from __future__ import annotations

from pathlib import Path

from .artifacts import sha256_json
from .source_model import BASE_COLLECTIONS
from .structure_store import ordered_chapters
from .write_sets import guard_observation, safe_project_path


def summary_inputs(root: Path, source: dict) -> dict:
    rows = {}
    from .source_impact import ReviewInputs
    from .source_evidence import read_evidence
    review_inputs = ReviewInputs(source)
    index = review_inputs.index
    citations = list(read_evidence(root)['batches'].values())
    for chapter in ordered_chapters(source):
        cid = chapter['chapterId']
        path = chapter.get('filePath') or f'正文/{cid}.md'
        state, _ = guard_observation(safe_project_path(root, path))
        from .source_plan import chapter_plan
        plan = chapter_plan(source, cid, _inputs=review_inputs)
        dependencies = sorted(row['id'] for row in plan['records'] if row['id'] != cid)
        plan_hash = plan['sourcePlanHash']
        found = set(dependencies)
        pending = [target for batch in citations if batch['chapterId'] == cid
                   for citation in batch['citations'] if citation['kind'] == 'depends_on'
                   for target in citation['targetIds']]
        citation_inputs = set()
        while pending:
            rid = pending.pop()
            if rid in citation_inputs:
                continue
            citation_inputs.add(rid)
            pending.extend(review_inputs.incoming[rid] - citation_inputs)
            owner = index.owner(rid)
            if owner and owner not in citation_inputs:
                pending.append(owner)
        dependencies = sorted((found | citation_inputs) - {cid})
        plan_hash = sha256_json({'plan': plan_hash, 'citationInputs': {
            rid: index.records.get(rid) for rid in sorted(citation_inputs)}})
        # Identity/order are compared separately; lifecycle and locator reseals
        # cannot force a semantic reread of unchanged chapter prose.
        metadata = {k: v for k, v in chapter.items() if k not in {
            'filePath', 'proseBinding', 'status', 'displayOrdinal', 'displayCode'}}
        rows[cid] = {'fileHash': state['sha256'], 'metadataHash': sha256_json(metadata),
                     'planHash': plan_hash, 'dependencyIds': dependencies}
    indexed = set(BASE_COLLECTIONS) | {'units', 'fieldDefinitions', 'attributes', 'fragments', 'presentations', 'relations'}
    ignored = {'revision', 'updatedAt', 'createdAt', 'schemaVersion', 'chapters', 'chapterArcLinks', 'references'}
    extra = {k: v for k, v in source.items() if k not in indexed | ignored}
    return {'version': 1, 'chapterOrder': list(rows), 'chapters': rows,
            'records': {rid: {'hash': sha256_json(row), 'kind': index.owner_types.get(rid)}
                        for rid, row in index.records.items() if rid not in rows},
            'globalHash': sha256_json(extra)}


def summary_delta(prior: dict | None, current: dict, covered: list[str]) -> dict:
    order = current['chapterOrder']
    if (not isinstance(prior, dict) or prior.get('version') != current['version']
            or not isinstance(prior.get('chapterOrder'), list)
            or not isinstance(prior.get('chapters'), dict) or not isinstance(prior.get('records'), dict)
            or not isinstance(prior.get('globalHash'), str)):
        return {'affectedChapterIds': order, 'fullRefresh': True, 'reason': 'dependency_snapshot_missing'}
    old_order = prior['chapterOrder']
    retained = [cid for cid in order if cid in old_order]
    # Appending new chapters is safe. Insertions, deletion and reordering change
    # causal context and require a full semantic reconciliation with the prior.
    topology_changed = retained != old_order or order[:len(old_order)] != old_order
    full = topology_changed or prior['globalHash'] != current['globalHash']
    affected = {cid for cid in order if cid not in covered or prior['chapters'].get(cid) != current['chapters'][cid]}
    old_records, records = prior['records'], current['records']
    changed = {rid for rid in old_records.keys() | records.keys() if old_records.get(rid) != records.get(rid)}
    for rid in changed:
        dependents = {cid for cid in order if rid in current['chapters'][cid]['dependencyIds']
                      or rid in prior['chapters'].get(cid, {}).get('dependencyIds', [])}
        if dependents:
            affected.update(dependents)
        elif rid not in old_records and records.get(rid, {}).get('kind') == 'time_anchor':
            # A new anchor absent from every recorded input has no earlier text
            # to reinterpret. Other unscoped changes remain uncertain.
            continue
        else:
            full = True
    return {'affectedChapterIds': order if full else [cid for cid in order if cid in affected],
            'fullRefresh': full, 'reason': 'scope_uncertain_or_order_changed' if full else 'bound_inputs_compared',
            'changedRecordIds': sorted(changed)}
