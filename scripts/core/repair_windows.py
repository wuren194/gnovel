"""Share repair windows once, with compact issues before pageable raw evidence."""
import json

from .artifacts import sha256_json
from .project import query_policy


def repair_materials(body, rows, paragraphs, *, anchor=None):
    radius = query_policy()['searchContextCharacters']
    maximum = query_policy()['defaultLimit']
    intervals = []
    localized = []
    for row in rows:
        spans = []
        for span in row['location']['spans']:
            bound = dict(span)
            if anchor is not None and span['start'] < span['end']:
                bound['anchorRef'] = anchor(span['start'], span['end'], [row['id']], 'location')
            spans.append(bound)
            intervals.append((max(0, span['start'] - radius), min(len(body), span['end'] + radius), row['id']))
        localized.append({**row, 'location': {**row['location'], 'spans': spans}})
    windows = []
    for start, end, identity in sorted(intervals):
        if windows and start <= windows[-1]['end']:
            windows[-1]['end'] = max(end, windows[-1]['end'])
            windows[-1]['issueIds'].add(identity)
        else:
            windows.append({'start': start, 'end': end, 'issueIds': {identity}})
    contexts, window_refs = [], {}
    for window in windows:
        start, end = window['start'], window['end']
        identity = 'window:' + sha256_json([start, end])
        window['id'] = identity
        for issue in window['issueIds']:
            window_refs.setdefault(issue, []).append(identity)
        # Linked issue IDs live in the anchor snapshot and the raw findings, not
        # unbounded window metadata repeated on every transport page.
        context = {'id': identity, 'source': 'contextWindow', 'fileStart': start, 'fileEnd': end,
                   'issueCount': len(window['issueIds']), 'location': {'state': 'window', 'spans': []},
                   'text': body[start:end]}
        if anchor is not None and start < end:
            context['anchorRef'] = anchor(start, end, window['issueIds'], 'window')
        contexts.append(context)
        measurements = [p for p in paragraphs if p['start'] < end and p['end'] > start]
        if measurements:
            contexts.append({'id': identity + ':measurements', 'source': 'paragraphCalibration',
                             'windowId': identity, 'advisory': True,
                             'location': {'state': 'measurement', 'spans': []},
                             'text': json.dumps(measurements, ensure_ascii=False, separators=(',', ':'))})

    def preview_span(span):
        return {**span, 'text': body[span['start']:min(span['end'], span['start'] + radius)],
                'textTruncated': span['end'] - span['start'] > radius}

    previews, raw_materials = [], []
    for row in localized:
        location, finding = row['location'], row['finding']
        summary = {k: finding[k] for k in ('ruleId', 'severity', 'advisory') if k in finding}
        raw = finding.get('raw') if isinstance(finding.get('raw'), dict) else {}
        message = next((source[key] for source in (finding, raw)
                        for key in ('summary', 'message', 'description', 'reason')
                        if isinstance(source.get(key), str) and source[key].strip()),
                       '需人工核对 ' + str(finding.get('ruleId') or row['source']))
        short = ' '.join(message.split())
        summary['summary'] = short[:radius]
        summary['summaryTruncated'] = len(short) > radius
        suggestion = finding.get('repairSuggestion')
        if isinstance(suggestion, str) and suggestion.strip():
            summary['repairSuggestion'] = suggestion[:radius]
            summary['repairSuggestionTruncated'] = len(suggestion) > radius
        # One compact card per occurrence keeps even highly ambiguous locations
        # pageable. Every occurrence is explicit; no "first match" is selected.
        for index, span in enumerate(location['spans'] or [None]):
            spans = [preview_span(span)] if span is not None else []
            refs = [window['id'] for window in windows if span is not None
                    and window['start'] <= span['start'] and span['end'] <= window['end']]
            previews.append({'id': f'{row["id"]}:preview:{index}', 'issueId': row['id'],
                             'source': 'issuePreview', 'issueSource': row['source'], 'finding': summary,
                             **{key: row[key] for key in ('reviewState',) if key in row},
                             'windowIds': refs, 'location': {**location, 'spans': spans,
                                 'occurrenceIndex': index if span is not None else None,
                                 'truncated': len(location['spans']) > len(spans)},
                             'text': summary['summary']})
        # Keep the original raw finding JSON shape, including arbitrarily long
        # quotes and all occurrence anchors. Only repeated display metadata is
        # bounded; joining text chunks restores the full original finding.
        spans = [preview_span(span) for span in location['spans'][:1]]
        raw_materials.append({'id': row['id'], 'source': row['source'], 'finding': summary,
                              'windowIds': window_refs.get(row['id'], [])[:maximum],
                              'windowIdsTruncated': len(window_refs.get(row['id'], [])) > maximum,
                              'location': {**location, 'spans': spans,
                                           'truncated': len(location['spans']) > len(spans)},
                              'text': json.dumps({'finding': finding, 'location': location,
                                                  'windowIds': window_refs.get(row['id'], []),
                                                  'additionalSources': row.get('additionalSources', []),
                                                  **{key: row[key] for key in ('reviewState', 'evidenceId', 'provenance',
                                                                               'comparison', 'previousDecision') if key in row}},
                                                 ensure_ascii=False, separators=(',', ':'))})
    by_window = {row['id']: row for row in contexts if row['source'] == 'contextWindow'}
    delivered, paired = set(), []
    for preview in sorted(previews, key=lambda row: not bool(row['windowIds'])):
        paired.append(preview)
        for window_id in preview['windowIds']:
            if window_id not in delivered:
                paired.append(by_window[window_id])
                delivered.add(window_id)
    measurements = [row for row in contexts if row['source'] == 'paragraphCalibration']
    return paired + raw_materials + measurements
