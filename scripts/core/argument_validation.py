"""One schema walker for fail-fast dispatch and bounded, read-only input preflight."""
from __future__ import annotations

import json
from pathlib import Path

from .structure_store import StructureError


def _tagged_branch(branches, arguments):
    """A shared required constant can identify a branch without guessing its shape."""
    if not isinstance(arguments, dict) or not branches or any(not isinstance(branch, dict) for branch in branches):
        return None
    fields = set.intersection(*(set(branch.get('required', [])) & set(branch.get('properties', {})) for branch in branches))
    for field in sorted(fields & arguments.keys()):
        definitions = [branch['properties'][field] for branch in branches]
        if any(not isinstance(definition, dict) or 'const' not in definition for definition in definitions):
            continue
        matching = [index for index, definition in enumerate(definitions)
                    if type(arguments[field]) is type(definition['const']) and arguments[field] == definition['const']]
        if len(matching) == 1:
            return matching[0], field
    return None


def argument_errors(schema: dict | bool, arguments, path: str = 'arguments'):
    def invalid(message, **details):
        return {'message': f'{path} {message}', 'path': path, **details}

    if isinstance(schema, bool):
        if not schema:
            yield invalid('is not allowed by this schema')
        return
    for branch in schema.get('allOf', []):
        yield from argument_errors(branch, arguments, path)
    for keyword in ('oneOf', 'anyOf'):
        if keyword not in schema:
            continue
        selected = _tagged_branch(schema[keyword], arguments)
        if selected is not None:
            index, field = selected
            for error in argument_errors(schema[keyword][index], arguments, path):
                yield {**error, 'schemaBranch': {'keyword': keyword, 'index': index, 'discriminator': field}}
            continue
        errors = [next(argument_errors(branch, arguments, path), None) for branch in schema[keyword]]
        matches = sum(error is None for error in errors)
        if not matches or keyword == 'oneOf' and matches != 1:
            yield invalid(f'does not match {keyword}', alternatives=[error for error in errors if error is not None],
                          descendantChecks='branch_shape_unresolved')
    types = schema.get('type')
    types = types if isinstance(types, list) else [types] if types else []
    checks = {'object': isinstance(arguments, dict), 'array': isinstance(arguments, list),
              'string': isinstance(arguments, str), 'integer': type(arguments) is int,
              'number': type(arguments) in (int, float), 'boolean': type(arguments) is bool, 'null': arguments is None}
    if types and not any(checks.get(t, False) for t in types):
        yield invalid('has the wrong type', expected=types, descendantChecks='parent_type_invalid')
        return
    if 'enum' in schema and not any(type(arguments) is type(value) and arguments == value for value in schema['enum']):
        yield invalid('is not an allowed value', allowed=schema['enum'])
    if 'const' in schema and (type(arguments) is not type(schema['const']) or arguments != schema['const']):
        yield invalid('requires an explicit constant', expected=schema['const'])
    if isinstance(arguments, str):
        if len(arguments) < schema.get('minLength', 0):
            yield invalid('is too short', minimumLength=schema['minLength'])
        if 'maxLength' in schema and len(arguments) > schema['maxLength']:
            yield invalid('is too long', maximumLength=schema['maxLength'])
    if type(arguments) in (int, float):
        if 'minimum' in schema and arguments < schema['minimum']:
            yield invalid('is below the minimum', minimum=schema['minimum'])
        if 'maximum' in schema and arguments > schema['maximum']:
            yield invalid('is above the maximum', maximum=schema['maximum'])
    if isinstance(arguments, dict):
        props = schema.get('properties', {})
        missing = set(schema.get('required', [])) - set(arguments)
        extra = set(arguments) - set(props)
        if missing or (extra and schema.get('additionalProperties') is False):
            yield invalid('has missing or unknown fields', missing=sorted(missing),
                          unknown=sorted(extra) if schema.get('additionalProperties') is False else [])
        for key, value in arguments.items():
            if key not in props and schema.get('additionalProperties') is False:
                continue
            child = props.get(key, schema.get('additionalProperties', {}))
            if isinstance(child, (dict, bool)):
                yield from argument_errors(child, value, path + '.' + key)
    if isinstance(arguments, list):
        if len(arguments) < schema.get('minItems', 0):
            yield invalid('requires more items', minimumItems=schema['minItems'])
        if 'maxItems' in schema and len(arguments) > schema['maxItems']:
            yield invalid('has too many items', maximumItems=schema['maxItems'])
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in arguments}) != len(arguments):
            yield invalid('contains duplicate items')
        if 'items' in schema:
            for index, value in enumerate(arguments):
                yield from argument_errors(schema['items'], value, f'{path}[{index}]')


def validate_arguments(schema: dict | bool, arguments, path: str = 'arguments') -> None:
    error = next(argument_errors(schema, arguments, path), None)
    if error is not None:
        raise StructureError('ARGUMENTS_INVALID', error['message'],
                             {key: value for key, value in error.items() if key != 'message'})


def preflight_arguments(root, *, operation: str, arguments: dict | None = None, validationRef: str | None = None,
                        limit=None, maxBytes=None, cursor=None):
    from .artifacts import sha256_json
    from .operations import operations
    from .project import query_policy
    from .static_cache import cached_sha256
    from .text_pages import text_page
    from .transport_cache import get, put
    spec = next((spec for spec in operations() if spec.id == operation), None)
    if spec is None:
        raise StructureError('OPERATION_NOT_FOUND', 'preflight requires a registered operation')
    policy = query_policy()
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    count = policy['defaultLimit'] if limit is None else limit
    if (type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail']
            or type(count) is not int or not 1 <= count <= policy['maxLimit']):
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid argument preflight budget')
    if (arguments is None) == (validationRef is None):
        raise StructureError('ARGUMENTS_PREFLIGHT_INPUT_INVALID', 'supply arguments or the original validationRef, not both')
    contract = sha256_json({'operation': operation, 'schema': spec.input_schema, 'effects': spec.effects, 'policy': policy,
                            'validator': cached_sha256(Path(__file__))})
    if validationRef is not None:
        saved = get(root, validationRef, kind='arguments-preflight')
        if saved['contract'] != contract:
            raise StructureError('ARGUMENTS_PREFLIGHT_STALE', 'operation schema or validator changed; preflight again')
        errors = saved['errors']
    else:
        supplied = dict(arguments)
        if 'projectRoot' in spec.input_schema.get('properties', {}):
            supplied.setdefault('projectRoot', str(root))
        errors = list(argument_errors(spec.input_schema, supplied))
        validationRef = put(root, 'arguments-preflight', {'contract': contract, 'arguments': supplied, 'errors': errors})
    rows = [{'id': f'error-{index}', 'text': json.dumps(error, ensure_ascii=False, separators=(',', ':'))}
            for index, error in enumerate(errors)]
    return text_page({'ok': True, 'code': 'ARGUMENTS_PREFLIGHT', 'operation': operation,
                      'validationRef': validationRef,
                      'schemaValid': not errors, 'errorCount': len(errors), 'targetEffects': spec.effects,
                      'executed': False, 'approvalGranted': False,
                      'coverage': {'scope': 'frozen arguments against the registered input schema',
                                   'domainChecks': 'not_run', 'unionBranches': 'alternatives_until_shape_is_resolved'}},
                     rows, key='errors', limit=count, budget=budget, cursor=cursor,
                     source=validationRef,
                     binding=sha256_json({'operation': operation, 'limit': count, 'maxBytes': budget}))
