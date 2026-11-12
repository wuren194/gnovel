"""Assemble model-supplied, quoted observations without generating literary claims."""
from copy import deepcopy

from .artifacts import sha256_json
from .evidence_quotes import quote_span
from .entity_registry import PREFIX_BY_TYPE
from .source_model import SourceIndex
from .structure_store import StructureError, new_id, validate_structure

REVIEW_FIELDS = ('facts', 'relationships', 'continuity', 'structure')
QUARANTINE_CATEGORIES = {'content_boundary', 'underage_explicit_content', 'other'}


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
    ids, generated, references, used = dict(identity_map or {}), {}, {}, set()
    index = SourceIndex.build(source)
    rows = {row['chapterId']: row for row in source['chapters']}
    citations, reviews = [], {}

    def allocate(handle, kind):
        rid = ids.get(handle) if identity_map is not None else new_id(kind)
        if not isinstance(rid, str) or rid in index.records or rid in generated.values():
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'invalid allocated identity')
        generated[handle] = rid
        references[handle] = rid
        return rid

    def quote(cid, value, target, reason):
        if cid not in materials:
            raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'quote must belong to selected chapters')
        try:
            span = quote_span(materials[cid]['text'], value)
        except StructureError as exc:
            if exc.code == 'EVIDENCE_QUOTE_AMBIGUOUS':
                exc.details['chapterId'] = cid
            raise
        citations.append({'chapterId': cid, **span, 'targetIds': [target], 'kind': 'depends_on', 'reason': reason})

    def topic(owner, key, label, body, handle, *, field_path=None, chapter_id=None):
        owner_kind = index.owner_types.get(owner) or next((e['entityType'] for e in proposed['entities'] if e['entityId'] == owner), None)
        if owner_kind is None:
            raise StructureError('IMPORT_ORGANIZATION_OWNER', 'select an existing owner or a declared entity reference', {
                'ref': owner,
                'fieldPath': field_path or 'ownerRef',
                'chapterId': chapter_id,
            })
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
        rid = allocate(handle, 'fact')
        proposed['fragments'].append({'fragmentId': rid, 'ownerId': owner, 'text': text(body, 'observation'),
            'order': 1 + max((r['order'] for r in proposed['fragments'] if r['ownerId'] == owner), default=0),
            'contentBindings': [attribute['attributeId']], 'dependsOn': [], 'contentRole': 'fact'})
        return rid

    for row in chapters:
        required = {'chapterId', 'synopsis', 'quotes', 'review', 'unresolved', 'existingRecordIds'}
        optional = {'adopt', 'quarantineReason', 'quarantineCategory'}
        if (not isinstance(row, dict) or required - set(row) or set(row) - required - optional
                or row['chapterId'] not in materials or row['chapterId'] in reviews):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'each selected chapter needs synopsis, quotes, review, unresolved and existingRecordIds')
        cid = row['chapterId']
        adopt = row.get('adopt', True)
        quarantine_reason = row.get('quarantineReason')
        quarantine_category = row.get('quarantineCategory')
        if type(adopt) is not bool:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'chapter adopt must be boolean',
                                 {'fieldPath': f'chapters[{len(reviews)}].adopt', 'chapterId': cid})
        if quarantine_reason is not None and (not isinstance(quarantine_reason, str) or not quarantine_reason.strip()):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'quarantineReason must be nonempty',
                                 {'fieldPath': f'chapters[{len(reviews)}].quarantineReason', 'chapterId': cid})
        if quarantine_category is not None and quarantine_category not in QUARANTINE_CATEGORIES:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'unsupported quarantine category',
                                 {'fieldPath': f'chapters[{len(reviews)}].quarantineCategory', 'chapterId': cid})
        if (quarantine_reason is None) != (quarantine_category is None):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'quarantine reason and category must be provided together',
                                 {'chapterId': cid,
                                  'fieldPath': f'chapters[{len(reviews)}].quarantineReason'
                                  if quarantine_reason is None else f'chapters[{len(reviews)}].quarantineCategory'})
        quarantined = quarantine_reason is not None
        if (not isinstance(row['review'], dict) or set(row['review']) != set(REVIEW_FIELDS)
                or not isinstance(row['unresolved'], list) or any(not isinstance(v, str) or not v.strip() for v in row['unresolved'])
                or not isinstance(row['quotes'], list) or not quarantined and not row['quotes']
                or not isinstance(row['existingRecordIds'], list) or any(rid not in index.records for rid in row['existingRecordIds'])):
            raise StructureError('IMPORT_REVIEW_INCOMPLETE', 'provide actual review conclusions, quotations and unresolved decisions')
        for key in REVIEW_FIELDS:
            text(row['review'][key], key)
        if accepted and adopt and not quarantined and row['unresolved']:
            raise StructureError('IMPORT_REVIEW_UNRESOLVED', 'resolve actual uncertainties before accepting historical chapters', {'chapterId': cid})
        synopsis = None
        if not quarantined:
            synopsis = topic(cid, 'import.retrospective', '原稿回溯梗概（非写作前计划）', row['synopsis'], '$synopsis-' + cid,
                             field_path=f'chapters[{len(reviews)}].chapterId', chapter_id=cid)
            for q in row['quotes']:
                quote(cid, q, synopsis, '原稿回溯梗概依据')
        reviews[cid] = {'review': row['review'], 'unresolved': row['unresolved'], 'synopsisId': synopsis,
                        'existingRecordIds': row['existingRecordIds'], 'adopt': adopt,
                        'quarantineReason': quarantine_reason, 'quarantineCategory': quarantine_category}
    if set(reviews) != set(materials):
        raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'review all and only selected chapters')

    def items(key):
        values = payload.get(key, [])
        if not isinstance(values, list):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', key + ' must be an array')
        return values

    for row in items('entities'):
        required = {'ref', 'entityType', 'name', 'summary', 'chapterId', 'quote'}
        optional = {'aliases', 'matchBy', 'entityId', 'createIfMissing'}
        if not isinstance(row, dict) or required - set(row) or set(row) - required - optional:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'entity fields differ from the help schema')
        if (not isinstance(row['ref'], str) or not row['ref'].startswith('$') or row['entityType'] not in PREFIX_BY_TYPE
                or row['entityType'] == 'time_anchor'):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'use a $local reference and a supported entity type')
        name = text(row['name'], 'entity.name')
        if row['chapterId'] not in reviews:
            raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'entity chapterId must name a selected chapter',
                                 {'chapterId': row['chapterId'], 'fieldPath': 'entities.chapterId'})
        if reviews[row['chapterId']]['quarantineReason'] is not None:
            raise StructureError('IMPORT_QUARANTINED_EVIDENCE', 'quarantined chapters cannot supply adopted entities',
                                 {'chapterId': row['chapterId'], 'fieldPath': 'entities.chapterId'})
        aliases = row.get('aliases', [])
        match_by = row.get('matchBy', ['name', 'alias'])
        create_if_missing = row.get('createIfMissing', True)
        if (not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
                or not isinstance(match_by, list) or set(match_by) - {'name', 'alias'}
                or type(create_if_missing) is not bool):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'entity aliases, matchBy, or createIfMissing is invalid',
                                 {'ref': row['ref']})
        selector = row.get('entityId')
        if selector is not None:
            selected = next((entity for entity in source['entities'] if entity['entityId'] == selector), None)
            if selected is not None and selected.get('status') == 'archived':
                raise StructureError('ARCHIVED_TARGET', 'canonical entity is archived',
                                     {'ref': row['ref'], 'entityId': selector,
                                      'fieldPath': f'entities[{len(generated)}].entityId',
                                      'chapterId': row['chapterId']})
        matches = [entity for entity in source['entities']
                   if entity.get('status') != 'archived' and entity['entityType'] == row['entityType']
                   and (selector == entity['entityId'] if selector else
                        'name' in match_by and entity['name'].strip().casefold() == name.strip().casefold()
                        or 'alias' in match_by and any(alias.strip().casefold() in
                            {entity['name'].strip().casefold(),
                             *(value.strip().casefold() for value in entity.get('aliases', []))}
                            for alias in aliases))]
        if len(matches) > 1:
            raise StructureError('IMPORT_ENTITY_AMBIGUOUS', 'canonical entity selector matches multiple identities', {
                'ref': row['ref'], 'fieldPath': f'entities[{len(generated)}].name',
                'candidates': [{'entityId': entity['entityId'], 'entityType': entity['entityType'],
                                'name': entity['name'], 'aliases': entity.get('aliases', [])}
                               for entity in matches]})
        if matches:
            entity = matches[0]
            rid = entity['entityId']
        else:
            if selector is not None or not create_if_missing:
                raise StructureError('IMPORT_ORGANIZATION_OWNER', 'canonical entity was not found and creation is disabled',
                                     {'ref': row['ref'], 'fieldPath': f'entities[{len(generated)}].name',
                                      'chapterId': row['chapterId'], 'matchBy': match_by,
                                      **({'entityId': selector} if selector is not None else {})})
            rid = allocate(row['ref'], row['entityType'])
            entity = {'entityId': rid, 'entityType': row['entityType'], 'name': name,
                      'summary': text(row['summary'], 'entity.summary'),
                      'aliases': list(dict.fromkeys(alias.strip() for alias in aliases)), 'status': 'active'}
            proposed['entities'].append(entity)
        if row['ref'] in used:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'duplicate local reference', {'ref': row['ref']})
        used.add(row['ref'])
        references[row['ref']] = rid
        quote(row['chapterId'], row['quote'], rid, '原稿明确的人物／对象资料')
    def resolve(ref, *, field_path, chapter_id):
        rid = references.get(ref, ref)
        if rid not in {e['entityId'] for e in proposed['entities']} and rid not in materials:
            raise StructureError('IMPORT_ORGANIZATION_OWNER', 'unknown entity or chapter reference', {
                'ref': ref,
                'fieldPath': field_path,
                'chapterId': chapter_id,
            })
        return rid
    for number, row in enumerate(items('facts')):
        if not isinstance(row, dict) or set(row) != {'ownerRef', 'text', 'chapterId', 'quote'}:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'fact fields differ from the help schema')
        if row['chapterId'] not in reviews:
            raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'fact chapterId must name a selected chapter',
                                 {'chapterId': row['chapterId'], 'fieldPath': f'facts[{number}].chapterId'})
        if reviews[row['chapterId']]['quarantineReason'] is not None:
            raise StructureError('IMPORT_QUARANTINED_EVIDENCE', 'quarantined chapters cannot supply adopted facts',
                                 {'chapterId': row['chapterId'], 'fieldPath': f'facts[{number}].chapterId'})
        rid = topic(resolve(row['ownerRef'], field_path=f'facts[{number}].ownerRef', chapter_id=row['chapterId']),
                    'import.observations', '原稿有据事实', row['text'], '$fact-' + str(number),
                    field_path=f'facts[{number}].ownerRef', chapter_id=row['chapterId'])
        quote(row['chapterId'], row['quote'], rid, '原稿事实依据')
    for number, row in enumerate(items('relations')):
        if not isinstance(row, dict) or set(row) != {'fromRef', 'toRef', 'kind', 'chapterId', 'quote', 'reason'}:
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'relation fields differ from the help schema')
        if row['chapterId'] not in reviews:
            raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'relation chapterId must name a selected chapter',
                                 {'chapterId': row['chapterId'], 'fieldPath': f'relations[{number}].chapterId'})
        if reviews[row['chapterId']]['quarantineReason'] is not None:
            raise StructureError('IMPORT_QUARANTINED_EVIDENCE', 'quarantined chapters cannot supply adopted relations',
                                 {'chapterId': row['chapterId'], 'fieldPath': f'relations[{number}].chapterId'})
        left = resolve(row['fromRef'], field_path=f'relations[{number}].fromRef', chapter_id=row['chapterId'])
        right = resolve(row['toRef'], field_path=f'relations[{number}].toRef', chapter_id=row['chapterId'])
        kind = text(row['kind'], 'relation.kind')
        existing = next((r for r in proposed['relations']
                         if (r['fromEntity'], r['toEntity'], r['kind']) == (left, right, kind)), None)
        if existing is not None:
            raise StructureError('IMPORT_ORGANIZATION_DUPLICATE', 'relation already exists', {
                'relationId': existing['relationId'],
                'sourceId': existing['fromEntity'],
                'targetId': existing['toEntity'],
                'kind': existing['kind'],
            })
        rid = allocate('$relation-' + str(number), 'relation')
        proposed['relations'].append({'relationId': rid, 'fromEntity': left, 'toEntity': right, 'kind': kind})
        quote(row['chapterId'], row['quote'], rid, text(row['reason'], 'relation.reason'))
    accepted_ids = {cid for cid, review in reviews.items() if review['adopt'] and review['quarantineReason'] is None}
    quarantined_ids = {cid for cid, review in reviews.items() if review['quarantineReason'] is not None}
    for chapter in proposed['chapters']:
        if chapter['chapterId'] in quarantined_ids:
            review = reviews[chapter['chapterId']]
            chapter.update(status='quarantined', quarantineReason=review['quarantineReason'],
                           quarantineCategory=review['quarantineCategory'])
        elif accepted and chapter['chapterId'] in accepted_ids:
            chapter.pop('quarantineReason', None)
            chapter.pop('quarantineCategory', None)
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
    return proposed, generated, references, citations, reviews
