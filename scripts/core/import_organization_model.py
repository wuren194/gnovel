"""Assemble model-supplied, quoted observations without generating literary claims."""
from copy import deepcopy

from .artifacts import sha256_json
from .evidence_quotes import quote_span
from .entity_registry import PREFIX_BY_TYPE
from .source_model import SourceIndex
from .structure_store import StructureError, new_id, validate_structure

REVIEW_FIELDS = ('facts', 'relationships', 'continuity', 'structure')


def text(value, label):
    from .review_artifacts import require_completed_text
    require_completed_text(value, label)
    return value


def assemble(source, payload, materials, identity_map=None):
    """Only append new semantic records and change explicitly accepted chapter status."""
    if not isinstance(payload, dict) or set(payload) - {'chapters', 'entities', 'facts', 'relations', 'acceptHistory'}:
        raise StructureError('IMPORT_ORGANIZATION_INVALID', 'unknown organization fields')
    accepted = payload.get('acceptHistory', False)
    if type(accepted) is not bool:
        raise StructureError('IMPORT_ORGANIZATION_INVALID', 'acceptHistory must be boolean')
    chapters = payload.get('chapters')
    if not isinstance(chapters, list) or not chapters:
        raise StructureError('IMPORT_ORGANIZATION_INVALID', 'provide actual chapter synopses and review conclusions')
    proposed = deepcopy(source)
    ids, generated, used = dict(identity_map or {}), {}, set()
    index = SourceIndex.build(source)
    rows = {row['chapterId']: row for row in source['chapters']}
    citations, reviews = [], {}

    def allocate(handle, kind):
        if handle in used:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'duplicate local reference', {'ref': handle})
        used.add(handle)
        rid = ids.get(handle) if identity_map is not None else new_id(kind)
        if not isinstance(rid, str) or rid in index.records or rid in generated.values():
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'invalid allocated identity')
        generated[handle] = rid
        return rid

    def quote(cid, value, target, reason):
        if cid not in materials:
            raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'quote must belong to selected chapters')
        span = quote_span(materials[cid]['text'], value)
        citations.append({'chapterId': cid, **span, 'targetIds': [target], 'kind': 'depends_on', 'reason': reason})

    def topic(owner, key, label, body, handle):
        owner_kind = index.owner_types.get(owner) or next((e['entityType'] for e in proposed['entities'] if e['entityId'] == owner), None)
        if owner_kind is None:
            raise StructureError('IMPORT_ORGANIZATION_OWNER', 'select an existing owner or a declared entity reference')
        field = next((row for row in proposed['fieldDefinitions'] if row['key'] == key), None)
        if field is None:
            field = {'fieldId': allocate('$field-' + key, 'field'), 'key': key, 'label': label,
                     'valueType': 'topic', 'appliesTo': ['chapter', *PREFIX_BY_TYPE]}
            proposed['fieldDefinitions'].append(field)
        if field['valueType'] != 'topic' or owner_kind not in field['appliesTo']:
            raise StructureError('IMPORT_ORGANIZATION_FIELD', 'existing retrospective field has incompatible semantics')
        attribute = next((row for row in proposed['attributes'] if row['ownerId'] == owner and row['fieldId'] == field['fieldId']), None)
        if attribute is None:
            attribute = {'attributeId': allocate('$attribute-' + owner + '-' + key, 'attribute'), 'ownerId': owner,
                         'fieldId': field['fieldId'], 'values': []}
            proposed['attributes'].append(attribute)
        matching = next((r for r in proposed['fragments'] if r['ownerId'] == owner and r['text'] == body
                         and r.get('status') != 'archived' and r['contentRole'] == 'fact'
                         and attribute['attributeId'] in r['contentBindings']), None)
        if matching is not None:
            return matching['fragmentId']
        rid = allocate(handle, 'fragment')
        proposed['fragments'].append({'fragmentId': rid, 'ownerId': owner, 'text': text(body, 'observation'),
            'order': 1 + max((r['order'] for r in proposed['fragments'] if r['ownerId'] == owner), default=0),
            'contentBindings': [attribute['attributeId']], 'dependsOn': [], 'contentRole': 'fact'})
        return rid

    for row in chapters:
        if (not isinstance(row, dict) or set(row) != {'chapterId', 'synopsis', 'quotes', 'review', 'unresolved', 'existingRecordIds'}
                or row['chapterId'] not in materials or row['chapterId'] in reviews):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'each selected chapter needs synopsis, quotes, review, unresolved and existingRecordIds')
        cid = row['chapterId']
        if (not isinstance(row['review'], dict) or set(row['review']) != set(REVIEW_FIELDS)
                or not isinstance(row['unresolved'], list) or any(not isinstance(v, str) or not v.strip() for v in row['unresolved'])
                or not isinstance(row['quotes'], list) or not row['quotes']
                or not isinstance(row['existingRecordIds'], list) or any(rid not in index.records for rid in row['existingRecordIds'])):
            raise StructureError('IMPORT_REVIEW_INCOMPLETE', 'provide actual review conclusions, quotations and unresolved decisions')
        for key in REVIEW_FIELDS:
            text(row['review'][key], key)
        if accepted and row['unresolved']:
            raise StructureError('IMPORT_REVIEW_UNRESOLVED', 'resolve actual uncertainties before accepting historical chapters', {'chapterId': cid})
        synopsis = topic(cid, 'import.retrospective', '原稿回溯梗概（非写作前计划）', row['synopsis'], '$synopsis-' + cid)
        for q in row['quotes']:
            quote(cid, q, synopsis, '原稿回溯梗概依据')
        reviews[cid] = {'review': row['review'], 'unresolved': row['unresolved'], 'synopsisId': synopsis,
                        'existingRecordIds': row['existingRecordIds']}
    if set(reviews) != set(materials):
        raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'review all and only selected chapters')

    def items(key):
        values = payload.get(key, [])
        if not isinstance(values, list):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', key + ' must be an array')
        return values

    for row in items('entities'):
        if not isinstance(row, dict) or set(row) != {'ref', 'entityType', 'name', 'summary', 'chapterId', 'quote'}:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'entity fields differ from the help schema')
        if (not isinstance(row['ref'], str) or not row['ref'].startswith('$') or row['entityType'] not in PREFIX_BY_TYPE
                or row['entityType'] == 'time_anchor'):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'use a $local reference and a supported entity type')
        name = text(row['name'], 'entity.name')
        if any(name in [e['name'], *e.get('aliases', [])] for e in proposed['entities']):
            raise StructureError('IMPORT_ENTITY_AMBIGUOUS', 'name already exists; use its stable ID for observations', {'name': name})
        rid = allocate(row['ref'], PREFIX_BY_TYPE[row['entityType']])
        proposed['entities'].append({'entityId': rid, 'entityType': row['entityType'], 'name': name,
                                     'summary': text(row['summary'], 'entity.summary'), 'aliases': [], 'status': 'active'})
        quote(row['chapterId'], row['quote'], rid, '原稿明确的人物／对象资料')
    def resolve(ref):
        rid = generated.get(ref, ref)
        if rid not in {e['entityId'] for e in proposed['entities']} and rid not in materials:
            raise StructureError('IMPORT_ORGANIZATION_OWNER', 'unknown entity or chapter reference', {'ref': ref})
        return rid
    for number, row in enumerate(items('facts')):
        if not isinstance(row, dict) or set(row) != {'ownerRef', 'text', 'chapterId', 'quote'}:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'fact fields differ from the help schema')
        rid = topic(resolve(row['ownerRef']), 'import.observations', '原稿有据事实', row['text'], '$fact-' + str(number))
        quote(row['chapterId'], row['quote'], rid, '原稿事实依据')
    for number, row in enumerate(items('relations')):
        if not isinstance(row, dict) or set(row) != {'fromRef', 'toRef', 'kind', 'chapterId', 'quote', 'reason'}:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'relation fields differ from the help schema')
        left, right, kind = resolve(row['fromRef']), resolve(row['toRef']), text(row['kind'], 'relation.kind')
        if any((r['fromEntity'], r['toEntity'], r['kind']) == (left, right, kind) for r in proposed['relations']):
            raise StructureError('IMPORT_ORGANIZATION_DUPLICATE', 'relation already exists')
        rid = allocate('$relation-' + str(number), 'relation')
        proposed['relations'].append({'relationId': rid, 'fromEntity': left, 'toEntity': right, 'kind': kind})
        quote(row['chapterId'], row['quote'], rid, text(row['reason'], 'relation.reason'))
    if accepted:
        for chapter in proposed['chapters']:
            if chapter['chapterId'] in reviews:
                chapter['status'] = 'stable'
    validate_structure(proposed)
    if identity_map is not None and generated != identity_map:
        raise StructureError('IMPORT_ORGANIZATION_INVALID', 'identity map is not the exact allocation set')
    final_index = SourceIndex.build(proposed)
    for cid, review in reviews.items():
        targets = set(review['existingRecordIds']) | {rid for q in citations if q['chapterId'] == cid for rid in q['targetIds']}
        # Include endpoints and the field/attribute identities consumed by facts.
        pending = list(targets)
        while pending:
            rid = pending.pop()
            record = final_index.records[rid]
            dependencies = set(record.get('contentBindings', [])) | set(record.get('dependsOn', []))
            dependencies.update(record[k] for k in ('fromEntity', 'toEntity', 'ownerId', 'fieldId') if k in record and record[k] != cid)
            pending.extend(dependencies - targets)
            targets.update(dependencies)
        review['recordHashes'] = {rid: sha256_json(final_index.records[rid]) for rid in sorted(targets)}
        review.update(proseHash=materials[cid]['proseHash'], bindingGeneration=rows[cid]['proseBinding']['generation'])
        from .import_history import chapter_review_shape
        review['chapterShapeHash'] = chapter_review_shape(proposed, next(c for c in proposed['chapters'] if c['chapterId'] == cid))
    return proposed, generated, citations, reviews
