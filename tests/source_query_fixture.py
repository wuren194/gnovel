"""Typed fields for query-focused synthetic tests; never production normalization."""
import hashlib


def declare_field(source, key, kind, *, value_type='text'):
    field = next((row for row in source['fieldDefinitions'] if row['key'] == key), None)
    if field is None:
        token = hashlib.sha256(key.encode()).hexdigest()[:12].upper()
        field = {'fieldId': 'field-QUERY-'+token, 'key': key, 'label': key,
                 'valueType': value_type, 'appliesTo': [kind]}
        source['fieldDefinitions'].append(field)
    elif kind not in field['appliesTo']:
        field['appliesTo'].append(kind)
    return field


def set_field(source, owner_id, key, value):
    owner = next(row for row in source['entities'] if row['entityId'] == owner_id)
    field = declare_field(source, key, owner['entityType'])
    attr = next((row for row in source['attributes'] if row['ownerId'] == owner_id and row['fieldId'] == field['fieldId']), None)
    if attr is None:
        token = hashlib.sha256((owner_id+':'+key).encode()).hexdigest()[:12].upper()
        attr = {'attributeId': 'attribute-QUERY-'+token, 'ownerId': owner_id, 'fieldId': field['fieldId'], 'values': []}
        source['attributes'].append(attr)
    attr['values'] = [{'valueId': attr['attributeId'].replace('attribute-', 'value-', 1), 'value': value}]
    return attr
