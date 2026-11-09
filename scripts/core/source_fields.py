"""Read-only field projections from typed source records."""
from __future__ import annotations

from collections import defaultdict

from .source_model import SourceIndex
from .source_selection import select_content
from .structure_store import StructureError, canonical_json


def field_keys(source: dict, requested: list[str] | None) -> set[str] | None:
    if not requested:
        return None
    found = set()
    for selector in requested:
        rows = []
        for key in ('fieldId', 'key', 'label'):
            rows = [r for r in source['fieldDefinitions'] if selector == r[key]]
            if rows:
                break
        if len(rows) != 1:
            raise StructureError('SOURCE_FIELD_SELECTOR_INVALID', 'field selector must identify exactly one source field', {'selector': selector})
        found.add(rows[0]['key'])
    return found


def field_specs(source: dict) -> dict:
    result = defaultdict(dict)
    for field in source['fieldDefinitions']:
        for kind in field['appliesTo']:
            result[kind][field['key']] = 'prose' if field['valueType'] == 'topic' else field['valueType']
    return dict(result)


def projections(source: dict) -> dict:
    index = SourceIndex.build(source)
    selected = select_content(source)
    result = {rid: {'fields': {}, 'fieldValues': {}, 'fragments': [], 'fieldPresence': set()}
              for rid in index.objects}
    values = defaultdict(lambda: defaultdict(list))
    for record in selected['records']:
        if record['kind'] == 'value':
            owner, key, content = record['ownerId'], record['fieldKey'], record['content']
            # Detail consumers use the same selected typed records, not a parse of
            # the text summary (which may itself contain JSON-looking prose).
            result[owner]['fieldValues'].setdefault(key, []).append(record)
            # Preserve validity and unknown states rather than inventing a current scalar.
            value = content.get('value')
            plain = (content.get('state', 'known') == 'known' and not content.get('validDuring')
                     and isinstance(value, str))
            values[owner][key].append(value if plain else canonical_json(content))
            if content.get('state', 'known') in {'known', 'not_applicable'}:
                result[owner]['fieldPresence'].add(key)
        else:
            fragment = record['content']
            by_owner = defaultdict(list)
            for aid in record['matchedBindings']:
                attr = index.attributes[aid]
                by_owner[attr['ownerId']].append(index.fields[attr['fieldId']]['key'])
            for owner, keys in by_owner.items():
                result[owner]['fragments'].append({'id': record['id'], 'fieldKeys': sorted(set(keys)), 'content': fragment})
                for key in keys:
                    result[owner]['fieldPresence'].add(key)
                    # Field summary points to prose, which is emitted once by fragment ID.
                    values[owner][key].append('source-fragment:' + record['id'])
    for owner, fields in values.items():
        result[owner]['fields'] = {key: '\n'.join(dict.fromkeys(items)) for key, items in fields.items()}
    return result


def query_settings_source(root, source, *, selector=None, kind=None, fields=None):
    from .entity_registry import resolve_entity
    from .project import query_policy
    from .query import _size
    wanted = field_keys(source, fields)
    all_values, specs = projections(source), field_specs(source)
    entities = source['entities']
    if selector is not None:
        try:
            entities = [resolve_entity(source, selector)]
        except StructureError as exc:
            if exc.code != 'ENTITY_NOT_FOUND': raise
            entities = []
    matches, kinds = [], {}
    for entity in entities:
        if kind is not None and entity['entityType'] != kind:
            continue
        projected = all_values[entity['entityId']]
        bucket = kinds.setdefault(entity['entityType'], {'cards': 0, 'fieldFill': {}})
        bucket['cards'] += 1
        for key in projected['fieldPresence']:
            bucket['fieldFill'][key] = bucket['fieldFill'].get(key, 0) + 1
        matches.append({'entityId': entity['entityId'], 'entityType': entity['entityType'], 'name': entity['name'],
            'path': None, 'startLine': None, 'endLine': None, 'sha256': None,
            'valueSource': '.gnovel/structure.json',
            'fields': {key: value for key, value in projected['fields'].items() if wanted is None or key in wanted},
            'fragments': [row for row in projected['fragments'] if wanted is None or wanted.intersection(row['fieldKeys'])],
            'unfilled': sorted(set(specs.get(entity['entityType'], {})) - projected['fieldPresence'])})
    result = {'selector': selector, 'kind': kind, 'fields': sorted(wanted) if wanted is not None else None,
              'cardCount': 0, 'matchedCount': len(matches), 'kinds': kinds, 'customFields': specs, 'matches': matches,
              'scope': {'matchedEntities': len(matches), 'cardCountBasis': 'no maintained cards in source mode'},
              'sourceMode': True, 'fieldKeyBasis': 'canonical source keys',
              'links': [{'tool': 'query_source', 'reason': 'paged selection and temporal queries'}]}
    if _size(result) > query_policy()['maxBytes']['detail']:
        raise StructureError('SOURCE_QUERY_REQUIRED', 'field result exceeds response budget; use paged source query')
    return result
