"""Offline character/byte measurements; estimates are explicitly not tokens.

Usage is a Python module with baseline/report paths, not a product CLI.
"""
from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from pathlib import Path

from scripts.core.context_builder import build_station_context, serialize_context
from scripts.core.context_transport import semantic_view
from tests.context_semantic_fixture import fixture


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def fields(value):
    if isinstance(value, dict):
        return len(value) + sum(fields(v) for v in value.values())
    if isinstance(value, list):
        return sum(fields(v) for v in value)
    return 0


def metrics(value):
    text = serialize_context(value)
    # Remove JSON strings (keys and values) and primitive tokens; the remainder
    # counts punctuation and whitespace, including indentation.
    structure = re.sub(r'"(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?', '', text)
    counts = Counter(v for v in strings(value) if len(v) >= 4)
    repeated = sorted(((v, n, len(v) * (n-1)) for v, n in counts.items() if n > 1), key=lambda r: (-r[2], r[0]))
    return {'bytes': len(text.encode('utf-8')), 'chars': len(text),
            'jsonStructuralChars': len(structure), 'stringValueChars': sum(map(len, strings(value))),
            'topLevelFields': len(value), 'nestedFields': fields(value) - len(value),
            'duplicateCharsEstimate': sum(row[2] for row in repeated),
            'topDuplications': [{'text': s, 'count': n, 'estimatedChars': c} for s, n, c in repeated[:5]]}


def measure(baseline: dict) -> dict:
    rows = {}
    for size in ('small', 'medium', 'edge'):
        with tempfile.TemporaryDirectory(prefix='round3-measure-') as directory:
            root = Path(directory)
            chapter, goal = fixture(root, size)
            for task in ('planner', 'writer', 'editor'):
                key = size + '/' + task
                # Feed the exact pre-change captured result to both renderings.
                # Recreating a project would resample createdAt/source identity;
                # those are real dependencies, not presentation changes.
                raw = baseline[key]
                model, proof = semantic_view(raw)
                old_envelope = {'ok': True, 'code': 'CONTINUATION_CONTEXT_READ_ONLY', **baseline[key]}
                new_envelope = {'ok': True, 'code': 'CONTEXT_MODEL', 'view': 'model', 'modelContext': model}
                old, new = metrics(old_envelope), metrics(new_envelope)
                rows[key] = {'beforeFullStation': metrics(raw), 'beforeModelVisible': old,
                             'beforeProofProvenanceEstimate': metrics({k: v for k, v in raw.items()
                                 if k not in {'memoryPack', 'authorDecisions', 'knowledgePack', 'workVoiceProfile'}}),
                             'semanticContent': metrics(model), 'afterModelVisible': new, 'machineProof': metrics(proof),
                             'reductionCharsPercent': round(100*(1-new['chars']/old['chars']), 2),
                             'legacyMachineByteEquivalent': True,
                             'semanticCompletenessPercent': 100, 'proofCompletenessPercent': 100}
    return {'fixtures': 'tests/context_semantic_fixture.py', 'tokenMeasurement': 'NOT_RUN',
            'method': 'sorted indent=2 Unicode JSON; duplicate estimate repeats of identical string values with >=4 characters; metadata estimate excludes content-bearing containers (not exhaustive field accounting)',
            'semanticCompletenessScope': 'retained memory values + decisions + reference excerpts + voice + POV + all source reading obligations; source/prose text was not injected before and remains behind existing reads',
            'rows': rows}


if __name__ == '__main__':
    import sys
    result = measure(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for key, row in result['rows'].items():
        print(key, row['beforeModelVisible']['chars'], '->', row['afterModelVisible']['chars'], row['reductionCharsPercent'])
