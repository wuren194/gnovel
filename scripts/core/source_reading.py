"""Text pages for the exact source queries selected by a preparation report."""
import json

from .artifacts import sha256_json, sha256_text
from .structure_store import StructureError, load_structure


def _pair_materials(source, query, report):
    from .pair_classification import pair_inputs
    from .source_model import snapshot_hash
    from .context_builder import _explicitly_visible

    inputs = pair_inputs(source)
    pairs = source.get('pairs', []) if query.get('includeArchived') else inputs['pairs']
    pairs = [row for row in pairs if query.get('pairId') is None or row['pairId'] == query['pairId']]
    pov = report['readingScope'].get('povCharacter')
    visible = [row for row in pairs if not pov or _explicitly_visible(row, pov)]
    rows = [{'id': row['pairId'], 'kind': 'pair', 'record': row}
            for row in sorted(visible, key=lambda row: row['pairId'])]
    if not pov:
        # The inventory judgment is author material; its reason may name private
        # pairs. Deliver it even for an explicitly empty inventory, never in POV.
        rows.insert(0, {'id': source['projectId'], 'kind': 'pair_assessment',
                        'assessment': inputs['assessment']})
    return {'snapshot': {'sourceHash': snapshot_hash(source), 'revision': source['revision']},
            'coverage': {'registeredPairSelectionComplete': True, 'povScoped': bool(pov),
                         'assessmentWithheld': bool(pov), 'omittedPov': len(pairs) - len(visible),
                         'semanticReviewComplete': False,
                         'knowledgeBasis': 'explicit pair visibility; membership and relevance do not grant visibility'},
            'warnings': ['视角读取只交付明确可见的配对；过滤为空不表示没有配对，库存评估仍为作者资料。'] if pov else []}, rows


def source_materials(root, query, report, request_index):
    tool = query['tool']
    source = load_structure(root)
    if tool == 'query_source':
        from .source_selection import select_project_content
        result = select_project_content(root, query.get('selection'))
        rows = result['records']
    elif tool == 'query_source_chapter_plan':
        from .source_plan import chapter_plan
        result = chapter_plan(source, query['chapterId'])
        rows = result['records']
        if report['readingScope'].get('povCharacter'):
            # The plan closure also contains Canon records. Deliver only authored
            # plan fragments here; Canon/beliefs use the prepared POV selection.
            rows = [r for r in rows if r['record'].get('contentRole') == 'plan']
            result['coverage'] = {**result['coverage'], 'povPlanOnly': True,
                                  'otherInputsUseScopedContentQuery': True}
    elif tool == 'query_source_rules':
        from .source_rules import evaluate_rules
        result = evaluate_rules(source, query['chapterId'], query['action'], root=root)
        rows = [{'id': r['ruleId'], **r} for r in result['items']]
    elif tool == 'query_source_pairs':
        result, rows = _pair_materials(source, query, report)
    else:
        raise StructureError('READ_BATCH_FORBIDDEN', 'query has no prepared text reader')
    chunks = []
    snapshot = sha256_json(result['snapshot'])
    for row in rows:
        # Retain all structured fields and epistemic roles. JSON fragments are
        # explicitly slices of one record and never independently parsed truth.
        text = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        path = f'{tool}/{row["id"]}'
        chunks.append({'id': sha256_json({'query': query, 'id': row['id'], 'snapshot': snapshot}),
                       'root': 'source', 'path': path, 'recordId': row['id'], 'fileSha256': snapshot,
                       'startLine': 1, 'endLine': 1, 'fileStart': 0,
                       'contentSha256': sha256_text(text), 'text': text, 'requestIndexes': [request_index]})
    return chunks, {'requestIndex': request_index, 'tool': tool,
                    'coverage': result['coverage'], 'warnings': result.get('warnings', [])}
