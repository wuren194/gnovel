"""Freeze field-level review text beside the plan, without copying it into story truth."""
from __future__ import annotations

from difflib import SequenceMatcher

from .project import query_policy
from .source_model import SourceIndex
from .structure_store import canonical_json


def _fields(old, new, path=()):
    if canonical_json(old) == canonical_json(new):
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            if key not in old or key not in new:
                yield list((*path, key)), old.get(key), new.get(key)
            else:
                yield from _fields(old[key], new[key], (*path, key))
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for index, (before, after) in enumerate(zip(old, new)):
            yield from _fields(before, after, (*path, index))
    else:
        yield list(path), old, new


def _text_edits(old, new):
    # Trim equal ends first, including long repetitive paragraph tails.
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-1-suffix] == new[-1-suffix]:
        suffix += 1
    a, b = old[prefix:len(old)-suffix], new[prefix:len(new)-suffix]
    for tag, i, j, k, l in SequenceMatcher(None, a, b).get_opcodes():
        if tag != 'equal':
            yield prefix+i, prefix+j, prefix+k, prefix+l


def source_diff(before: dict, after: dict, *, patches: dict | None = None) -> list[dict]:
    old, new = SourceIndex.build(before), SourceIndex.build(after)
    old_records = {**old.records, **{r['decisionId']: r for r in before.get('authorDecisions', [])},
                   **{r['candidateId']: r for r in before.get('planningCandidates', [])}}
    new_records = {**new.records, **{r['decisionId']: r for r in after.get('authorDecisions', [])},
                   **{r['candidateId']: r for r in after.get('planningCandidates', [])}}
    # The project owns singleton guidance too. Include it in the review projection
    # without expanding SourceIndex's fact/dependency graph or inventing another ID.
    for source, records in ((before, old_records), (after, new_records)):
        if 'workVoiceProfile' in source:
            project_id = source['projectId']
            records[project_id] = {**records[project_id], 'workVoiceProfile': source['workVoiceProfile']}
    context = query_policy()['searchContextCharacters']
    rows = []
    for rid in sorted(old_records.keys() | new_records.keys()):
        prior, current = old_records.get(rid), new_records.get(rid)
        if canonical_json(prior) == canonical_json(current):
            continue
        # Values are already nested in attributes; emit each authored change once.
        if rid in old.values or rid in new.values:
            continue
        owner = new.owner(rid) or old.owner(rid) or rid
        owner_row = new.objects.get(owner, old.objects.get(owner, {}))
        metadata = {'recordId': rid, 'ownerId': owner,
                    'ownerLabel': owner_row.get('name', owner_row.get('title', owner)),
                    'contentRole': (current or prior).get('contentRole'),
                    'status': (current or prior).get('status', 'active')}
        for path, a, b in _fields(prior, current):
            # State transitions are whole values, not prose substring edits.
            if isinstance(a, str) and isinstance(b, str) and path[-1:] != ['status']:
                anchors = [item for item in (patches or {}).get(rid, []) if item['op'] == 'text' and item['path'] == path]
                edits = []
                if anchors:
                    delta = 0
                    for item in sorted(anchors, key=lambda item: a.index(item['original'])):
                        start = a.index(item['original'])
                        end = start + len(item['original'])
                        edits.append((start, end, start+delta, start+delta+len(item['replacement'])))
                        delta += len(item['replacement']) - len(item['original'])
                else:
                    edits = _text_edits(a, b)
                for start, end, new_start, new_end in edits:
                    rows.append({**metadata, 'path': path, 'before': a[start:end], 'after': b[new_start:new_end],
                                 'beforeRange': {'start': start, 'end': end},
                                 'afterRange': {'start': new_start, 'end': new_end},
                                 'contextBefore': a[max(0, start-context):start],
                                 'contextAfter': a[end:end+context]})
            else:
                rows.append({**metadata, 'path': path, 'before': a, 'after': b})
    return rows


def markdown_change(row: dict) -> str:
    from .structure_store import canonical_json
    path = '.'.join(map(str, row['path'])) or '(record)'
    def text(value):
        return value if isinstance(value, str) else canonical_json(value)
    before, after = text(row['before']), text(row['after'])
    # Indented blocks keep arbitrary authored backticks out of the report's Markdown syntax.
    return (f"### {row['ownerLabel']} · {row['recordId']} · {path}\n\n"
            f"身份：{row.get('contentRole') or 'record'}；状态：{row['status']}\n\n"
            '修改前：\n\n' + '\n'.join('    ' + line for line in before.split('\n')) + '\n\n'
            '修改后：\n\n' + '\n'.join('    ' + line for line in after.split('\n')) + '\n\n'
            f"前文：{row.get('contextBefore', '')}\n\n后文：{row.get('contextAfter', '')}\n")
