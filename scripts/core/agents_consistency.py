"""Cross-check explicit decision citations in project instructions against source state."""
from __future__ import annotations

import re

from .author_decisions import _decision_stale
from .project import source_model_policy
from .write_sets import digest, safe_project_path


def check_agents_decisions(root, source):
    path = safe_project_path(root, 'AGENTS.md')
    coverage = {'basis': 'explicit author decision IDs in AGENTS.md', 'semanticConsistencyChecked': False}
    if not path.is_file():
        return {'present': False, 'references': [], 'issues': [], 'coverage': coverage}
    data = path.read_bytes()
    by_id = {row['decisionId']: row for row in source.get('authorDecisions', [])}
    policy = source_model_policy()
    prefix = policy['authorDecisions']['idPrefix']
    pattern = re.compile(r'(?<![A-Za-z0-9_-])' + re.escape(prefix) + '-' + policy['idSuffixPattern'] + r'(?![A-Za-z0-9_-])')
    references, issues = [], []
    for line_number, text in enumerate(data.decode('utf-8').splitlines(), 1):
        for rid in dict.fromkeys(pattern.findall(text)):
            decision = by_id.get(rid)
            state = decision['status'] if decision else 'missing'
            stale = bool(decision and state == 'active' and _decision_stale(source, by_id, rid))
            row = {'decisionId': rid, 'status': state, 'stale': stale,
                   'path': 'AGENTS.md', 'line': line_number, 'text': text,
                   'supersededBy': sorted(item['decisionId'] for item in by_id.values() if item.get('supersedes') == rid)}
            references.append(row)
            if state != 'active' or stale:
                issues.append({'code': 'AGENTS_DECISION_REFERENCE_STALE' if decision else 'AGENTS_DECISION_REFERENCE_UNKNOWN',
                               **row})
    return {'present': True, 'sha256': digest(data), 'references': references, 'issues': issues, 'coverage': coverage}
