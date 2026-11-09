"""Locate current evidence or conservatively rebase historical repair locations."""
from bisect import bisect_right

from .repair_history import map_span


def quote_text(finding):
    raw = finding.get('raw') if isinstance(finding.get('raw'), dict) else {}
    return next((value for value in [finding.get(key) for key in ('location', 'evidence', 'text', 'match')]
                 + [raw.get(key) for key in ('matched', 'quote', 'match')]
                 if isinstance(value, str) and value.strip()), None)


def location(body, finding, *, exact_only=False):
    starts = [0, *(index + 1 for index, char in enumerate(body) if char == '\n')]
    quote = quote_text(finding)
    raw = finding.get('raw') if isinstance(finding.get('raw'), dict) else {}
    line = finding.get('line') or raw.get('line')
    spans, state = [], 'missing'
    if quote:
        position = 0
        while (position := body.find(quote, position)) >= 0:
            spans.append((position, position + len(quote)))
            position += 1
        if not exact_only and len(spans) > 1 and type(line) is int and 1 <= line <= len(starts):
            left = starts[line - 1]
            right = starts[line] if line < len(starts) else len(body)
            local = [span for span in spans if left <= span[0] < right]
            if len(local) == 1:
                spans = local
        state = 'unique' if len(spans) == 1 else 'ambiguous' if spans else 'missing'
    elif not exact_only and type(line) is int and 1 <= line <= len(starts):
        spans = [(starts[line - 1], starts[line] if line < len(starts) else len(body))]
        state = 'line'
    start, end = finding.get('start'), finding.get('end')
    if not exact_only and type(start) is int and type(end) is int and 0 <= start < end <= len(body):
        if quote is None or body[start:end] == quote:
            spans, state = [(start, end)], 'unique'
    return {'state': state, 'occurrences': len(spans), 'spans': [
        {'start': start, 'end': end, 'startLine': bisect_right(starts, start),
         'endLine': bisect_right(starts, max(start, end - 1))} for start, end in spans]}


def historical_location(body, finding, issue_id, before_hash, after_hash, bodies, transitions):
    before = bodies.get(before_hash)
    prior = location(before, finding) if before is not None else None
    comparison = {'beforeBodyHash': before_hash, 'beforeBodyAvailable': before is not None,
                  'before': ([{**span, 'text': before[span['start']:span['end']]} for span in prior['spans']]
                             if prior else [{'text': quote_text(finding)}]),
                  'afterBodyHash': after_hash, 'after': [], 'patchReceipts': []}
    if prior and before_hash == after_hash:
        current = prior
    elif prior and len(prior['spans']) == 1:
        span = prior['spans'][0]
        mapped = map_span(span['start'], span['end'], issue_id, before_hash, after_hash, transitions)
        if mapped is not None:
            start, end = mapped['start'], mapped['end']
            current = location(body, {'start': start, 'end': end}) if start < end else {
                'state': 'patched', 'occurrences': 1, 'spans': [{'start': start, 'end': end}]}
            current['state'] = mapped['state']
            comparison['patchReceipts'] = mapped['receipts']
        else:
            current = location(body, {'location': before[span['start']:span['end']]}, exact_only=True)
    else:
        current = location(body, finding, exact_only=True)
    if current['state'] in {'missing', 'ambiguous'}:
        current = {'state': 'needs_location', 'occurrences': current['occurrences'], 'spans': []}
    comparison['after'] = [{**span, 'text': body[span['start']:span['end']]} for span in current['spans']]
    return current, comparison
