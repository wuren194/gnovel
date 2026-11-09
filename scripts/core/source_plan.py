"""Scoped chapter planning inputs, independent of disposable reading exports."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .source_impact import ReviewInputs
from .source_placement import chapter_volume_id
from .source_review import bounded_page, fingerprint, source_snapshot
from .structure_store import StructureError


def base_chapter_plan(source: dict, chapter_id: str, *, _inputs: ReviewInputs | None = None) -> dict:
    inputs = _inputs or ReviewInputs(source)
    index = inputs.index
    if not isinstance(chapter_id, str) or index.owner_types.get(chapter_id) != 'chapter':
        raise StructureError('CHAPTER_NOT_FOUND', 'unknown chapter')
    chapter = index.objects[chapter_id]
    scope = {source['projectId'], chapter_id}
    volume_id = chapter_volume_id(chapter, source.get('schemaVersion'))
    if volume_id is not None:
        scope.add(volume_id)
    # Units the chapter belongs to (schema 3 members, or schema-2 unitId) are planning owners.
    for unit_id in index.units_for(chapter_id):
        scope.add(unit_id)
    links = [row for row in source['chapterArcLinks'] if row['chapterId'] == chapter_id]
    scope.update(row['arcId'] for row in links)
    # Ancestor arc planning applies even when only the leaf arc is mounted.
    pending = list(scope)
    while pending:
        row = index.objects.get(pending.pop(), {})
        parent = row.get('parentArcId')
        if parent and parent not in scope:
            scope.add(parent); pending.append(parent)
    fragments = {rid for rid, row in index.fragments.items()
                 if row['ownerId'] in scope and row.get('status') != 'archived'
                 and (row['ownerId'] == chapter_id or row['contentRole'] == 'plan')}
    local_plans = sorted(rid for rid in fragments if index.fragments[rid]['ownerId'] == chapter_id
                         and index.fragments[rid]['contentRole'] == 'plan')
    references = [row for row in source.get('references', []) if row['sourceId'] == chapter_id]
    roots = fragments | scope | {row['targetId'] for row in references}
    # A registered chapter → entity reference selects that card's content, including
    # mixed fragments bound to its attributes. Merely sharing a project/volume owner
    # is not a content reference. Precise fragment dependencies remain precise.
    referenced_owners = {row['targetId'] for row in references}
    referenced_attributes = {rid for rid, row in index.attributes.items() if row['ownerId'] in referenced_owners}
    roots.update(referenced_attributes)
    roots.update(rid for rid, row in index.fragments.items() if row.get('status') != 'archived' and
                 (row['ownerId'] in referenced_owners or referenced_attributes.intersection(row['contentBindings'])))
    found = inputs.closure(roots, include_owners=True)
    from .source_rules import applicable_rules
    rule_scope = scope | {rid for rid, temporal in found.items() if not temporal}
    rules = applicable_rules(source, rule_scope, rules=inputs.rules)
    found = inputs.closure(roots | {row['entityId'] for row in rules}, include_owners=True)
    records = []
    for rid in sorted(found):
        record = deepcopy(inputs.content(rid, found[rid], authored=True))
        records.append({'id': rid, 'record': record,
                        **({'dependencyRole': 'time_constraint'} if found[rid] else {})})
    recipe = {'kind': 'chapter-plan-v2', 'chapterId': chapter_id,
              'scopeIds': sorted(scope), 'ruleScopeIds': sorted(rule_scope),
              'ruleIds': sorted(row['entityId'] for row in rules),
              'chapterArcLinks': sorted(links, key=lambda r: r['arcId']),
              'references': sorted(references, key=lambda r: (r['targetId'], r.get('kind', '')))}
    from .pair_classification import pair_inputs
    recipe['pairInputHash'] = pair_inputs(source, chapter_id)['inputHash']
    from .prompt_conditions import chapter_condition_binding
    recipe['conditionalContracts'] = chapter_condition_binding(source, chapter_id)
    return {'chapterId': chapter_id, 'sourcePlanHash': fingerprint({'recipe': recipe, 'records': records}),
            'snapshot': {'sourceHash': inputs.source_hash, 'revision': source['revision']},
            'recipe': recipe, 'records': records, 'planFragmentIds': local_plans,
            'ready': bool(local_plans),
            'coverage': {'scope': 'chapter fragments and ancestor plans plus registered input closure',
                         'pairScope': 'declared project-wide pairs plus pairs scoped to chapter, volume or ancestor arcs',
                         'ancestorFactsRequireReference': True,
                         'unregisteredDependenciesExcluded': True, 'proseScanned': False,
                         'appSemanticReviewRequired': True}}


def chapter_plan(source: dict, chapter_id: str, *, _inputs: ReviewInputs | None = None) -> dict:
    from .author_decisions import chapter_author_inputs
    result = base_chapter_plan(source, chapter_id, _inputs=_inputs)
    author_inputs = chapter_author_inputs(source, chapter_id, result)
    result['authorInputs'] = author_inputs
    result['recipe']['authorInputs'] = author_inputs
    result['sourcePlanHash'] = fingerprint({'recipe': result['recipe'], 'records': result['records']})
    return result


def query_chapter_plan(root: Path, *, chapterId: str, view: str = 'list', limit=None, maxBytes=None, cursor=None) -> dict:
    result = chapter_plan(source_snapshot(root), chapterId)
    if view not in {'list', 'detail'}:
        raise StructureError('QUERY_VIEW_INVALID', 'chapter plan view must be list or detail')
    rows = result['records'] if view == 'detail' else [
        {'id': row['id'], 'contentHash': fingerprint(row['record']),
         'kind': row['record'].get('contentRole', row['record'].get('entityType', 'source_record'))}
        for row in result['records']]
    return bounded_page(rows, snapshot=result['snapshot'], selection={'chapterId': chapterId, 'view': view},
                        code='SOURCE_CHAPTER_PLAN', limit=limit, maxBytes=maxBytes, cursor=cursor,
                        chapterId=chapterId, sourcePlanHash=result['sourcePlanHash'], ready=result['ready'],
                        planFragmentCount=len(result['planFragmentIds']), coverage=result['coverage'])


def outline_binding(root: Path, source: dict, chapter: dict) -> dict:
    """One binding contract used by lifecycle, preparation and publication."""
    result = chapter_plan(source, chapter['chapterId'])
    if not result['ready']:
        raise StructureError('SOURCE_CHAPTER_PLAN_MISSING', 'chapter has no active authored plan fragment',
                             {'chapterId': chapter['chapterId']})
    unavailable = result['authorInputs']['unavailableDecisions']
    if unavailable:
        raise StructureError('AUTHOR_DECISIONS_REQUIRE_REVIEW',
            'applicable author decisions require explicit reviewed refresh or withdrawal before writing',
            {'chapterId': chapter['chapterId'], 'decisions': unavailable,
             'readAction': {'operation': 'source.author-decisions', 'arguments': {'status': 'active'}}})
    return {'kind': 'source', 'field': 'sourcePlanHash', 'hash': result['sourcePlanHash']}
