"""Literal term matching and owner resolution inside an already selected source."""
from collections import Counter
from bisect import bisect_right
import json

from .structure_store import StructureError


def validate_terms(terms, literal_terms):
    if (not isinstance(terms, list) or not terms
            or any(not isinstance(term, str) or not term for term in terms)
            or type(literal_terms) is not bool):
        raise StructureError('SOURCE_SEARCH_INVALID', 'terms must be nonempty literal strings')
    if not literal_terms:
        for term in terms:
            try:
                value = json.loads(term)
            except ValueError:
                continue
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                raise StructureError('SOURCE_SEARCH_TERMS_FORMAT',
                                     'terms contains a JSON array as one literal; repeat --terms for CLI values',
                                     {'literalTerms': True, 'hint': 'Use literalTerms only to search the brackets and quotes themselves.'})
    return sorted(set(terms))


def selected_owners(result, source, selectors):
    if selectors is None:
        return None
    if not isinstance(selectors, list) or not selectors or any(not isinstance(x, str) or not x for x in selectors):
        raise StructureError('SOURCE_SEARCH_OWNER_INVALID', 'ownerSelectors must be a nonempty string list')
    from .entity_registry import match_labels, require_resolution_budget
    from .source_model import SourceIndex
    from .source_selection import metadata_visible
    index = SourceIndex.build(source)
    visible = {row['id']: row for row in result['owners']}
    labels = {}
    for rid, owner in visible.items():
        record = index.objects[rid]
        labels[rid] = [owner['label']]
        if metadata_visible(record, result['selection']):
            labels[rid].extend(record.get('aliases', []))
    found = set()
    for selector in selectors:
        matches = match_labels(selector, labels)
        if len(matches) != 1:
            details = {'selector': selector, 'candidateIds': matches,
                       'candidates': [visible[rid] for rid in matches]}
            require_resolution_budget(details)
            raise StructureError('SOURCE_SEARCH_OWNER_AMBIGUOUS' if matches else 'SOURCE_SEARCH_OWNER_NOT_FOUND',
                                 'resolve an owner within the current selection; the scope is not widened',
                                 details)
        found.add(matches[0])
    return found


def literal_hits(text, terms, mode, near):
    hits = []
    for term in terms:
        start = text.find(term)
        while start >= 0:
            hits.append((start, start + len(term), term))
            start = text.find(term, start + 1)
    hits.sort()
    if mode == 'any':
        return hits
    if {row[2] for row in hits} != set(terms):
        return []
    if mode == 'all':
        return hits
    # Sweep qualifying windows, then locate each hit in that sorted window set.
    # No Cartesian product of occurrences or repeated copying of active hits.
    by_end = sorted(range(len(hits)), key=lambda i: hits[i][1])
    counts, active, starts = Counter(), set(), []
    right = 0
    for left, (start, _, _) in enumerate(hits):
        while right < len(by_end) and hits[by_end[right]][1] <= start + near:
            index = by_end[right]
            if index >= left:
                active.add(index)
                counts[hits[index][2]] += 1
            right += 1
        if len(counts) == len(terms):
            starts.append(start)
        if left in active:
            active.remove(left)
            counts[hits[left][2]] -= 1
            if not counts[hits[left][2]]:
                del counts[hits[left][2]]
    accepted = []
    for row in hits:
        index = bisect_right(starts, row[0]) - 1
        if index >= 0 and starts[index] >= row[1] - near:
            accepted.append(row)
    return accepted
