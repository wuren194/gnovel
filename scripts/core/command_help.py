"""Project-free command help derived from the public operation catalogue."""
from __future__ import annotations

from copy import deepcopy
import shlex

from .project import TOOLBOX_ROOT, query_policy
from .structure_store import StructureError


def cli_action(operation, arguments, **metadata):
    """Return an actionable transport mapping, without dispatching any operation."""
    from .operations import operations, validate_arguments
    from .cli_examples import command_example
    spec = next(spec for spec in operations() if spec.id == operation)
    validate_arguments(spec.input_schema, arguments)
    # A JSON false cannot in general be represented by omitting a store_true
    # flag: e.g. closeout.prepareNext defaults to true in its domain handler.
    # Preserve the user's exact argument instead of silently changing meaning.
    false_fields = [key for key, value in arguments.items()
                    if value is False and spec.input_schema['properties'][key].get('type') == 'boolean']
    if false_fields:
        raise StructureError('CLI_EXPLICIT_FALSE_UNSUPPORTED',
            'CLI omission cannot guarantee an explicit false; use the identical MCP call',
            {'fields': false_fields, 'mcpAction': {'tool': spec.name, 'arguments': arguments}, 'executionPerformed': False})
    prefix = shlex.join(['python3', str(TOOLBOX_ROOT / 'scripts/novelctl.py')])
    command = command_example(spec, arguments).replace('python3 scripts/novelctl.py', prefix, 1)
    return {'operation': operation, 'tool': spec.name, 'arguments': arguments,
            'cli': command, 'cliArgv': shlex.split(command), **metadata}


def _present_help(report: dict, *, view: str, arguments: dict) -> dict:
    """Keep the exact call contract; CLI examples and repeated guidance are opt-in."""
    result = deepcopy(report)
    result['view'] = view
    if view == 'full':
        actions = [result.get('readAction'), result.get('noMatchAction')]
        actions.extend(result.get('lookup', {}).values())
        actions.extend(row.get('helpAction') for row in result.get('groups', []))
        actions.extend(row.get('helpAction') for row in result.get('operations', []))
        for action in actions:
            if isinstance(action, dict) and action.get('tool') == 'get_tool_help':
                action['arguments']['view'] = 'full'
        return result
    for key in ('capabilities', 'scripts'):
        if result.get(key) == []:
            result.pop(key)
    for key in ('cliHelp', 'cliExamples'):
        result.pop(key, None)
    guide = result.get('help')
    if isinstance(guide, dict):
        for key in ('cliScript', 'commandHelp'):
            guide.pop(key, None)
        # The opening help owns general instructions; exact lookups need the
        # operation's description and contract, not another copy of the guide.
        if result.get('code') != 'COMMAND_HELP':
            guide.pop('instructions', None)
        if not guide:
            result.pop('help')
    start = result.get('start')
    if isinstance(start, dict):
        start.pop('cli', None)
    for row in result.get('operations', []):
        # A CLI-only operation still needs its executable route.
        if row.get('mcp') == row.get('name') and row.get('name'):
            row.pop('mcp')
            row.pop('cli', None)
        if row.get('tool'):
            row.pop('command', None)
        schema = row.get('inputSchema')
        if isinstance(schema, dict) and row.get('required') == schema.get('required', []):
            row.pop('required', None)
    result.setdefault('authorization', '发现操作不构成执行或作者批准授权')
    if result.get('noMatchAction') is None:
        result.pop('noMatchAction', None)
    result['fullHelpAction'] = {'tool': 'get_tool_help', 'arguments': {**arguments, 'view': 'full'}}
    return result


def command_help(*, operation: str | None = None, query: str | None = None,
                 commandGroup: str | None = None, offset: int = 0, limit: int | None = None,
                 arguments=None, view: str = 'model') -> dict:
    from .operations import operations
    from .tool_catalog import list_capabilities
    from .cli_examples import command_example
    from .query import _size
    specs = operations()
    group = commandGroup
    policy = query_policy()
    limit = policy['defaultLimit'] if limit is None else limit
    if (view not in ('model', 'full')
            or sum(value is not None for value in (operation, query, group)) > 1
            or arguments is not None and operation is None
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= policy['maxLimit']):
        raise StructureError('ARGUMENTS_INVALID', 'choose operation, query or group; use model/full view and valid pagination')
    full_arguments = {key: value for key, value in {
        'operation': operation, 'query': query, 'commandGroup': group, 'arguments': arguments,
        'offset': offset, 'limit': limit,
    }.items() if value is not None and (key not in ('offset', 'limit') or group is not None)}

    def present(report):
        return _present_help(report, view=view, arguments=full_arguments)
    cli = str(TOOLBOX_ROOT / 'scripts/novelctl.py')
    prefix = shlex.join(['python3', cli])
    guide = {
        'cliScript': cli,
        'commandHelp': prefix + ' project tools help',
        'instructions': [
            '不熟悉命令时查 help；已持有当前定义时直接调用，无须每步重查。',
            '项目查询、准备、修改和提交使用公开命令；MCP 与 CLI 均为工具箱入口。',
            '续写先 task.prepare(task=continue, view=model)，按当前状态返回材料、缺项与下一步。',
            '读取准备结果的材料与分页引用；不自行解析内部 JSON 来代替状态、归属或就绪判断。',
            '帮助和示例不执行操作、不授予批准；缺项或错误沿返回动作处理，不用临时脚本绕过。',
        ],
    }
    if query is not None:
        result = list_capabilities(query=query)
        return present({**result, 'help': guide,
                'noMatchAction': {'tool': 'get_tool_help', 'arguments': {}} if not result['operations'] else None})
    if operation is not None:
        normalized = ' '.join(operation.split())
        spec = next((spec for spec in specs if normalized in
                     (spec.id, spec.name, ' '.join(spec.cli), 'project ' + ' '.join(spec.cli))), None)
        if spec is None:
            raise StructureError('OPERATION_NOT_FOUND', 'unknown operation ID, MCP name or CLI command',
                                 {'helpAction': {'tool': 'get_tool_help', 'arguments': {}}})
        result = list_capabilities(operation=spec.id)
        examples = [command_example(spec, example) for example in spec.examples]
        rendered = {}
        if arguments is not None:
            action = cli_action(spec.id, arguments)
            rendered = {'cliCommand': action['cli'], 'cliArgv': action['cliArgv'], 'executionPerformed': False}
        return present({**result, 'help': guide, **rendered,
                'cliHelp': prefix + ' project ' + shlex.join(spec.cli) + ' --help',
                'cliExamples': [example.replace('python3 scripts/novelctl.py', prefix, 1) for example in examples]})
    groups = sorted({spec.cli[0] for spec in specs})
    if group is None:
        continuation = next(spec for spec in specs if spec.id == 'task.prepare')
        return present({'ok': True, 'code': 'COMMAND_HELP', 'help': guide,
                'start': {'operation': continuation.id, 'tool': continuation.name,
                          'arguments': {'task': 'continue', 'view': 'model'},
                          'projectRootRequired': True,
                          'cli': prefix + ' project task prepare --project-root <项目目录> --task continue --view model'},
                'lookup': {'byIntent': {'tool': 'get_tool_help', 'arguments': {'query': '续写这个'}},
                           'byOperation': {'tool': 'get_tool_help', 'arguments': {'operation': 'task.prepare'}}},
                'groups': [{'group': name, 'operationCount': sum(spec.cli[0] == name for spec in specs),
                            'helpAction': {'tool': 'get_tool_help', 'arguments': {'commandGroup': name}}} for name in groups]})
    if group not in groups:
        raise StructureError('ARGUMENTS_INVALID', 'unknown command group', {'groups': groups})
    selected = sorted((spec for spec in specs if spec.cli[0] == group), key=lambda spec: spec.cli)
    if offset > len(selected):
        raise StructureError('ARGUMENTS_INVALID', 'offset exceeds the command count')
    result = {'ok': True, 'code': 'COMMAND_GROUP_HELP', 'group': group, 'total': len(selected),
              'operations': [], 'nextOffset': None, 'help': guide}
    for index in range(offset, min(offset + limit, len(selected))):
        spec = selected[index]
        row = {'id': spec.id, 'tool': spec.name, 'command': 'project ' + ' '.join(spec.cli),
               'description': spec.description, 'effects': spec.effects,
               'helpAction': {'tool': 'get_tool_help', 'arguments': {'operation': spec.id}}}
        result['operations'].append(row)
        following = index + 1
        result['nextOffset'] = following if following < len(selected) else None
        if following < len(selected):
            result['readAction'] = {'tool': 'get_tool_help', 'arguments': {'commandGroup': group, 'offset': following, 'limit': limit}}
        else:
            result.pop('readAction', None)
        if _size(present(result)) > policy['maxBytes']['list']:
            result['operations'].pop()
            if not result['operations']:
                raise StructureError('OPERATION_DEFINITION_TOO_LARGE', 'query the exact operation', {'operation': spec.id})
            result['nextOffset'] = index
            result['readAction'] = {'tool': 'get_tool_help', 'arguments': {'commandGroup': group, 'offset': index, 'limit': limit}}
            break
    return present(result)
