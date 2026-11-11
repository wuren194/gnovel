"""Current source identities: type prefixes and opaque, stable suffixes."""
from __future__ import annotations

import re
import uuid

from .project import source_model_policy


def identity_prefixes() -> dict[str, str]:
    policy = source_model_policy()
    return {**policy['entityPrefixes'], **policy['objectPrefixes'],
            **policy['fragmentPrefixes'],
            **policy['receiptPrefixes'],
            **{row['idField'].removesuffix('Id'): row['prefix']
               for name, row in policy['collections'].items() if name != 'fragments'},
            'value': policy['valueIdPrefix'],
            'planning_candidate': policy['planningCandidates']['idPrefix'],
            'author_decision': policy['authorDecisions']['idPrefix'],
            'pair': policy['pairClassification']['idPrefix']}


def identity_pattern(kind: str) -> re.Pattern:
    policy = source_model_policy()
    if kind == 'entity':
        prefixes = policy['entityPrefixes'].values()
    elif kind == 'fragment':
        prefixes = policy['fragmentPrefixes'].values()
    else:
        prefixes = [identity_prefixes()[kind]]
    return re.compile('^(?:' + '|'.join(map(re.escape, prefixes)) + ')-' + policy['idSuffixPattern'] + '$')


def new_id(kind: str, *, content_role: str | None = None) -> str:
    if kind == 'fragment':
        prefix = source_model_policy()['fragmentPrefixes'][content_role]
    else:
        prefix = identity_prefixes()[kind]
    suffix = uuid.uuid4().hex[:source_model_policy()['generatedSuffixLength']]
    return f'{prefix}-{suffix}'
