"""Literal source-fragment search; matches are evidence locations, not conclusions."""
from __future__ import annotations

from pathlib import Path

from .project import query_policy
from .source_review import bounded_page
from .source_selection import select_project_content
from .structure_store import StructureError
from .write_sets import digest


def search_source(root: Path, *, terms: list[str], selection: dict | None = None,
                  contextCharacters: int | None = None, limit=None, maxBytes=None, cursor=None,
                  ownerSelectors=None, matchMode='any', nearCharacters=None, literalTerms=False, view='full',
                  _term_source=None, _code='SOURCE_SEARCH') -> dict:
    policy = query_policy()
    from .source_search_match import validate_terms, selected_owners, literal_hits
    terms = validate_terms(terms, literalTerms)
    if view not in {'full', 'model'} or matchMode not in {'any', 'all', 'near'}:
        raise StructureError('SOURCE_SEARCH_INVALID', 'invalid search view or matchMode')
    if ((matchMode == 'near' and (type(nearCharacters) is not int or not 0 <= nearCharacters <= policy['maxBytes']['detail']))
            or matchMode != 'near' and nearCharacters is not None):
        raise StructureError('SOURCE_SEARCH_INVALID', 'near requires an explicit bounded nearCharacters; other modes omit it')
    context = policy['searchContextCharacters'] if contextCharacters is None else contextCharacters
    if type(context) is not int or not 0 <= context <= policy['maxBytes']['detail']:
        raise StructureError('SOURCE_SEARCH_INVALID', 'invalid context character budget')
    result = select_project_content(root, selection)
    if _term_source is not None:
        result['snapshot'] = {**result['snapshot'], 'termSource': _term_source}
    owners = None
    if ownerSelectors is not None:
        from .source_review import source_snapshot
        from .source_model import snapshot_hash
        source = source_snapshot(root)
        if snapshot_hash(source) != result['snapshot']['sourceHash']:
            raise StructureError('CURSOR_STALE', 'source changed while resolving search owners')
        owners = selected_owners(result, source, ownerSelectors)
    rows = []
    fragments = [row for row in result['records'] if row['kind'] == 'fragment'
                 and (owners is None or row['content']['ownerId'] in owners)]
    for row in fragments:
        fragment = row['content']
        text = fragment['text']
        content_hash = digest(text.encode())
        for start, end, term in literal_hits(text, terms, matchMode, nearCharacters):
            left, right = max(0, start - context), min(len(text), end + context)
            rows.append({'fragmentId': row['id'], 'ownerId': fragment['ownerId'],
                             'ownerLabel': result['referenceLabels'].get(fragment['ownerId'], fragment['ownerId']),
                             'selectedOwnerId': row['ownerId'], 'contentRole': fragment['contentRole'],
                             'status': fragment.get('status', 'active'),
                             'factEvidenceEligible': fragment['contentRole'] == 'fact' and fragment.get('status') != 'archived',
                             'evidenceIneligibilityReason': ('archived' if fragment.get('status') == 'archived' else
                                                            'non_fact_fragment' if fragment['contentRole'] != 'fact' else None),
                             'reviewStatus': row['reviewStatus'], 'term': term,
                             'start': start, 'end': end, 'contentHash': content_hash,
                             'context': {'start': left, 'end': right, 'text': text[left:right]},
                             'locator': {'fragmentId': row['id'], 'offset': left,
                                         'sourceHash': result['snapshot']['sourceHash']}})
    rows.sort(key=lambda row: (row['fragmentId'], row['start'], row['term']))
    query = {'terms': terms, 'selection': result['selection'], 'contextCharacters': context,
             'matchMode': matchMode, 'nearCharacters': nearCharacters, 'ownerSelectors': ownerSelectors,
             'literalTerms': literalTerms, 'view': view}
    details = {}
    if owners is not None:
        details['ownerResolution'] = {'ownerIds': sorted(owners), 'scope': 'within the original selection'}
    if view == 'model':
        from .transport_cache import put
        reference = put(root, 'source-search', {'snapshot': result['snapshot'], 'query': query, 'items': rows})
        details.update(searchRef=reference, detailsAction={'operation': 'task.snapshot', 'arguments': {'reference': reference}})
        rows = [{key: value for key, value in row.items() if key not in {'contentHash', 'locator', 'selectedOwnerId'}} for row in rows]
    return bounded_page(rows, snapshot=result['snapshot'], selection=query, query=query,
                        code=_code, limit=limit, maxBytes=maxBytes, cursor=cursor,
                        coverage={**result['coverage'], 'scannedFragmentCount': len(fragments),
                                  'annotationsExcluded': False, 'semanticJudgmentMade': False,
                                  'factEvidenceEligibilityBasis': 'active fact role; target relevance still checked by proposal validation',
                                  'absenceMeaning': 'no literal match in selected fragments; not semantic consistency'},
                        warnings=result['warnings'], **details)
