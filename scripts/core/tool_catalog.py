"""Runtime capability catalogue; policy metadata never supplies executable code."""
from __future__ import annotations

import re

from .static_cache import cached_json
from .operations import operations
from .project import TOOLBOX_ROOT, query_policy, tool_discovery_policy, toolbox_manifest
from .query import _size
from .structure_store import StructureError

FACT_TYPES = {
    'project.initialized': bool, 'candidate.ready': bool, 'diagnostics.current': bool,
    'review.current': bool, 'summary.stale': bool, 'timeline.hasInversion': bool,
    'review.hasLocalFinding': bool,
}
BOUNDARIES = {'chapter.review', 'chapter.finalize'}
AUDIENCES = {'author', 'composer', 'router', 'planner', 'writer', 'editor', 'titler', 'project-maintenance', 'repository-maintenance'}


def load_catalog() -> dict:
    path = TOOLBOX_ROOT / toolbox_manifest()['sharedProtocols']['toolRegistry']
    data = cached_json(path)
    if data.get('schemaVersion') != 3:
        raise StructureError('TOOL_CATALOG_INVALID', 'unsupported capability catalogue schema')
    specs = {s.id: s for s in operations()}
    if set(tool_discovery_policy()['intents']) - set(specs):
        raise StructureError('TOOL_CATALOG_INVALID', 'discovery intent names an unregistered operation')
    ids = set()
    for cap in data.get('capabilities', []):
        required = {'id', 'description', 'binding', 'audiences', 'tasks', 'when', 'requiredFor', 'evidence'}
        if set(cap) != required or cap['id'] in ids:
            raise StructureError('TOOL_CATALOG_INVALID', 'invalid or duplicate capability')
        ids.add(cap['id'])
        if not cap['description'] or not isinstance(cap['audiences'], list) or not set(cap['audiences']) <= AUDIENCES:
            raise StructureError('TOOL_CATALOG_INVALID', 'invalid capability audience or description')
        if not isinstance(cap['tasks'], list) or not cap['tasks'] or not set(cap['tasks']) <= {'planner', 'writer', 'editor', 'repair', 'titler', 'continuity'}:
            raise StructureError('TOOL_CATALOG_INVALID', 'invalid capability tasks')
        when = cap['when']
        if set(when) != {'fact', 'op', 'value', 'reason'} or when['fact'] not in FACT_TYPES or when['op'] not in ('eq', 'ne') or type(when['value']) is not FACT_TYPES[when['fact']]:
            raise StructureError('TOOL_CATALOG_INVALID', 'unknown fact, operator or value type')
        if not isinstance(cap['requiredFor'], list) or not set(cap['requiredFor']) <= BOUNDARIES:
            raise StructureError('TOOL_CATALOG_INVALID', 'unknown evidence boundary')
        if cap['evidence'] not in (None, 'diagnostics', 'stage_b_review') or (cap['requiredFor'] and not cap['evidence']):
            raise StructureError('TOOL_CATALOG_INVALID', 'necessary capability has no evidence consumer')
        binding = cap['binding']
        if set(binding) != {'kind', 'ref'}:
            raise StructureError('TOOL_CATALOG_INVALID', 'invalid binding')
        if binding['kind'] == 'operation':
            if binding['ref'] not in specs: raise StructureError('TOOL_CATALOG_INVALID', 'unknown operation')
        elif binding['kind'] == 'skill':
            path = (TOOLBOX_ROOT / binding['ref']).resolve()
            if not path.is_relative_to(TOOLBOX_ROOT) or path.name != 'SKILL.md' or not path.is_file():
                raise StructureError('TOOL_CATALOG_INVALID', 'skill binding is not an allowlisted SKILL.md')
        else:
            raise StructureError('TOOL_CATALOG_INVALID', 'capability binding must be skill or operation')
    tools = {row['id']: row for row in data['tools']}
    for tool in tools.values():
        if tool['kind'] not in ('operation', 'internal', 'maintenance') or not (TOOLBOX_ROOT / tool['path']).is_file():
            raise StructureError('TOOL_CATALOG_INVALID', 'invalid script inventory')
        if tool['kind'] == 'internal' and tool.get('parent') not in tools:
            raise StructureError('TOOL_CATALOG_INVALID', 'internal checker has no parent')
    return data


def _definition(spec):
    bridge = ('capabilities.read' if spec.effects == 'read' else
              'capabilities.export' if spec.effects == 'export-write' else None)
    entry = (next((row for row in operations() if row.id == bridge), None)
             if bridge and spec.mcp and spec.id not in {'capabilities.list', 'capabilities.read', 'capabilities.export'} else None)
    return {'id': spec.id, 'name': spec.name, 'description': spec.description,
            'cli': ['project', *spec.cli], 'mcp': spec.name if spec.mcp else None,
            'effects': spec.effects, 'category': spec.category,
            'annotations': spec.tool()['annotations'],
            'required': spec.input_schema.get('required', []), 'inputSchema': spec.input_schema,
            'examples': list(spec.examples),
            'invocation': ({'tool': entry.name, 'operation': spec.id,
                            'arguments': {}, 'projectRootRequired': True} if entry else
                           {'tool': spec.name if spec.mcp else None, 'cliOnly': not spec.mcp})}


def _term_matches(query, term):
    term = term.casefold()
    if term.isascii() and any(char.isalnum() for char in term):
        return re.search(r'(?<![a-z0-9_])' + re.escape(term) + r'(?![a-z0-9_])', query) is not None
    return term in query


def _search_definitions(specs, query, *, audience, task, category):
    policy = tool_discovery_policy()
    if not isinstance(query, str) or not query.strip() or len(query) > policy['maxQueryCharacters']:
        raise StructureError('ARGUMENTS_INVALID', 'query must be a nonempty bounded intent',
                             {'path': 'arguments.query', 'maxLength': policy['maxQueryCharacters']})
    text, matches = query.casefold(), []
    for spec in specs.values():
        if category not in (None, 'operation'):
            continue
        if audience and spec.audiences and audience not in spec.audiences:
            continue
        if task and spec.tasks and task not in spec.tasks:
            continue
        identities = (spec.id, spec.name, 'project ' + ' '.join(spec.cli))
        exact = text.strip() in identities or any(_term_matches(text, identity)
                                                  for identity in identities if not identity.isalpha())
        groups = policy['intents'].get(spec.id, [])
        terms = [[term for term in group if _term_matches(text, term)] for group in groups]
        if not exact and (not terms or not all(terms)):
            continue
        strength = sum(max(map(len, group)) for group in terms if group)
        matches.append((not exact, -strength, spec.id, spec, terms))
    matches.sort(key=lambda row: row[:3])
    response = {'ok': True, 'code': 'OPERATION_SEARCH', 'query': query, 'operations': [],
                'capabilities': [], 'scripts': [], 'matched': len(matches), 'hasMore': False,
                'authorization': '仅匹配显式词组，不执行、不推测项目模式或批准。按用途和参数选择后单独调用。',
                'next': {'tool': 'list_capabilities', 'arguments': {'operation': '<id>'}}}
    for _, _, _, spec, terms in matches[:policy['maxResults']]:
        row = {**_definition(spec), 'matchedTerms': [term for group in terms for term in group]}
        response['operations'].append(row)
        if _size(response) > query_policy()['maxBytes']['detail']:
            response['operations'].pop()
            break
    response['hasMore'] = len(response['operations']) < len(matches)
    if matches and not response['operations']:
        raise StructureError('OPERATION_DEFINITION_TOO_LARGE', 'use exact operation lookup for this definition',
                             {'operation': matches[0][3].id})
    if _size(response) > query_policy()['maxBytes']['detail']:
        raise StructureError('QUERY_BUDGET_INVALID', 'discovery response exceeds configured byte budget')
    return response


def invoke_discovered(root, *, operation, arguments, effects):
    from .operations import call_operation
    spec = next((row for row in operations() if row.id == operation), None)
    if (spec is None or not spec.mcp or spec.effects != effects
            or spec.id in {'capabilities.list', 'capabilities.read', 'capabilities.export'}):
        raise StructureError('OPERATION_NOT_ALLOWED', 'this bridge only calls registered operations of its declared effect')
    if not isinstance(arguments, dict) or 'projectRoot' in arguments:
        raise StructureError('ARGUMENTS_INVALID', 'arguments must omit projectRoot; select the root at the bridge boundary',
                             {'path': 'arguments.arguments.projectRoot' if isinstance(arguments, dict) else 'arguments.arguments'})
    from .context_transport import model_default_arguments
    return call_operation(operation, root, model_default_arguments(operation, arguments))


def list_capabilities(*, audience: str | None = None, category: str | None = None,
                      task: str | None = None, view: str = 'model', operation: str | None = None,
                      query: str | None = None) -> dict:
    data = load_catalog()
    if audience is not None and audience not in AUDIENCES:
        raise StructureError('ARGUMENTS_INVALID', 'unknown capability audience')
    if category is not None and category not in ('operation', 'skill', 'internal', 'maintenance'):
        raise StructureError('ARGUMENTS_INVALID', 'unknown capability category')
    if view not in {'model', 'audit'} or (task is not None and task not in {'planner', 'writer', 'editor', 'repair', 'titler', 'continuity'}):
        raise StructureError('ARGUMENTS_INVALID', 'unknown discovery view or task')
    specs = {s.id: s for s in operations()}
    if query is not None:
        if operation is not None or view != 'model':
            raise StructureError('ARGUMENTS_INVALID', 'intent query cannot be combined with exact operation or audit view')
        return _search_definitions(specs, query, audience=audience, task=task, category=category)
    if operation is not None:
        spec = specs.get(operation)
        if spec is None:
            raise StructureError('OPERATION_NOT_FOUND', 'operation is not registered', {'operation': operation})
        return {'ok': True, 'code': 'OPERATION_DEFINITION', 'view': view,
                'schemaVersion': data['schemaVersion'], 'capabilities': [], 'scripts': [],
                'operations': [_definition(spec)],
                'authorization': '发现操作不构成执行或作者批准授权'}
    rows = []
    for cap in data['capabilities']:
        if audience and audience not in cap['audiences']: continue
        if task and task not in cap['tasks']: continue
        if category and category != cap['binding']['kind']: continue
        row = dict(cap)
        if cap['binding']['kind'] == 'operation':
            spec = specs[cap['binding']['ref']]
            row['operation'] = {'id': spec.id, 'mcp': spec.name if spec.mcp else None,
                                'cli': ['project', *spec.cli], 'effects': spec.effects}
            if view == 'audit':
                row['operation']['inputSchema'] = spec.input_schema
        rows.append(row)
    bound = {row['binding']['ref'] for row in rows if row['binding']['kind'] == 'operation'}
    selected = [s for s in specs.values() if view == 'audit' or s.id in bound or s.discover or (
        s.audiences and (not audience or audience in s.audiences) and (not task or task in s.tasks))]
    return {'ok': True, 'code': 'CAPABILITY_CATALOG', 'view': view,
            'schemaVersion': data['schemaVersion'], 'capabilities': rows,
            'scripts': [row for row in data['tools'] if (view == 'audit' or category in {'internal', 'maintenance'})
                        and (not category or category == row['kind'])],
            'operations': [{'id': s.id, 'name': s.name, 'cli': ['project', *s.cli], 'effects': s.effects,
                            'category': s.category, 'inputSchema': s.input_schema}
                           for s in selected if not category or category == 'operation'],
            'expansion': {'operation': 'capabilities.list', 'view': 'audit'}}
