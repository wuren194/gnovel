#!/usr/bin/env python3
"""Self-contained first-use setup for a copied skill or a complete Govel plugin."""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile

SCRIPT = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT.parent))
from engine_install import BindError, install_engine
from host_registration import inspect_registration, install_registration


def read_policy() -> dict:
    try:
        policy = json.loads((SCRIPT.parent.parent / 'binding.json').read_text(encoding='utf-8'))
        limits = ('handshakeTimeoutSeconds', 'downloadTimeoutSeconds', 'downloadMaxBytes',
                  'archiveMaxBytes', 'archiveMaxFiles')
        if (not isinstance(policy, dict) or policy.get('schemaVersion') != 1 or policy.get('repository') != 'wuren194/govel'
                or not isinstance(policy.get('ref'), str)
                or not isinstance(policy.get('pythonMinimum'), list)
                or len(policy['pythonMinimum']) != 2
                or any(type(value) is not int or value < 0 for value in policy['pythonMinimum'])
                or type(policy.get('projectSchemaVersion')) is not int
                or any(type(policy.get(key)) is not int or policy[key] < 1 for key in limits)):
            raise ValueError('invalid policy')
        return policy
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise BindError('BIND_POLICY_INVALID', '接入包缺少有效 binding.json；重新安装完整 govel-bind Skill') from exc


def require_runtime(policy: dict) -> None:
    minimum = tuple(policy['pythonMinimum'])
    if sys.version_info < minimum:
        raise BindError('PYTHON_UNSUPPORTED', 'Govel 需要 Python ' + '.'.join(map(str, minimum)) + ' 或更新版本',
                        pythonExecutable=sys.executable)
    if platform.system() not in {'Darwin', 'Linux'}:
        raise BindError('PLATFORM_UNSUPPORTED', 'Govel 需要 macOS 或 Linux；Windows 请使用 WSL2 的 Linux 环境')


def engine_info(root: Path, policy: dict) -> dict:
    root = root.expanduser().resolve()
    try:
        manifest = json.loads((root / 'toolbox.manifest.json').read_text(encoding='utf-8'))
        required = ('SKILL.md', 'scripts/mcp_server.py', 'scripts/novelctl.py',
                    'scripts/core/project.py', 'shared/registries/tools.json', 'shared/registries/workflow.json')
        if (not isinstance(manifest, dict) or manifest.get('name') != 'govel' or manifest.get('schemaVersion') != policy['projectSchemaVersion']
                or not isinstance(manifest.get('version'), str)
                or not all((root / name).is_file() for name in required)
                or not (root / 'shared/contracts').is_dir()):
            raise ValueError('incomplete or unsupported engine')
        publisher = root / 'scripts/core/antigravity_registration.py'
        if not publisher.is_file() or 'publish_host_configuration' not in {
                node.name for node in ast.parse(publisher.read_text(encoding='utf-8')).body
                if isinstance(node, ast.FunctionDef)}:
            raise BindError('ENGINE_UPDATE_REQUIRED', '所选引擎缺少当前接入发布接口；请更新完整 Govel 引擎',
                            engineRoot=str(root))
    except (OSError, ValueError, TypeError, SyntaxError) as exc:
        raise BindError('ENGINE_INVALID', '目录不是完整的当前格式 Govel 引擎', engineRoot=str(root)) from exc
    return {'engineRoot': str(root), 'version': manifest['version'], 'projectSchemaVersion': manifest['schemaVersion']}


def state_path(value: str | None) -> Path:
    path = Path(value).expanduser() if value else Path.home() / '.local/share/govel'
    if path.is_symlink():
        raise BindError('BIND_STATE_UNSAFE', '绑定目录不能是符号链接')
    return path.absolute().resolve()


def read_binding(state_root: Path) -> dict | None:
    path = state_root / 'binding.json'
    if path.is_symlink():
        raise BindError('BIND_STATE_UNSAFE', '绑定记录不能是符号链接')
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or value.get('schemaVersion') != 1
                or not isinstance(value.get('engineRoot'), str) or not Path(value['engineRoot']).is_absolute()):
            raise ValueError('invalid binding')
        return value
    except (OSError, ValueError) as exc:
        raise BindError('BIND_STATE_INVALID', '已有绑定记录无法读取；未自动覆盖或重新下载') from exc


def locate_engine(explicit: str | None, state_root: Path, policy: dict) -> tuple[dict | None, str | None]:
    if explicit:
        return engine_info(Path(explicit), policy), None
    bundled = SCRIPT.parents[3]
    if (bundled / 'toolbox.manifest.json').exists():
        return engine_info(bundled, policy), None
    saved = read_binding(state_root)
    if saved:
        return engine_info(Path(saved['engineRoot']), policy), saved.get('commit')
    return None, None


def data_environment(engine: dict, state_root: Path, host: str) -> tuple[dict, dict]:
    explicit = {key: str(Path(os.environ[key]).expanduser().resolve())
                for key in ('GOVEL_DATA_HOME', 'GOVEL_PROVIDER_CONFIG') if os.environ.get(key)}
    if explicit:
        provider = explicit.get('GOVEL_PROVIDER_CONFIG')
        return explicit, {'dataHome': None if provider else explicit.get('GOVEL_DATA_HOME'),
                          'dataHomeSource': 'provider_configuration' if provider else 'environment',
                          'providerConfig': provider}
    root = Path(engine['engineRoot'])
    if host != 'plugin' and (root / '.git').exists():
        return {}, {'dataHome': str(root / 'local'), 'dataHomeSource': 'checkout'}
    data_home = str(state_root / 'data')
    return {'GOVEL_DATA_HOME': data_home}, {'dataHome': data_home, 'dataHomeSource': 'stable_user_directory'}


def registered_data(engine: dict, entry: dict) -> dict:
    """Describe this direct-server probe without inventing unregistered defaults."""
    keys = ('GOVEL_DATA_HOME', 'GOVEL_PROVIDER_CONFIG')
    inherited = {key: os.environ.get(key, '') for key in keys}
    environment = {**inherited, **entry.get('env', {})}
    source = 'host_configuration' if any(key in entry.get('env', {}) for key in keys) else 'inherited_environment'
    cwd = Path(entry.get('cwd') or engine['engineRoot'])
    def absolute(value):
        path = Path(value).expanduser()
        return str((path if path.is_absolute() else cwd / path).resolve())
    if environment.get('GOVEL_PROVIDER_CONFIG'):
        return {'dataHome': None, 'providerConfig': absolute(environment['GOVEL_PROVIDER_CONFIG']),
                'dataHomeSource': source}
    if environment.get('GOVEL_DATA_HOME'):
        return {'dataHome': absolute(environment['GOVEL_DATA_HOME']), 'dataHomeSource': source}
    return {'dataHome': str(Path(engine['engineRoot']) / 'local'), 'dataHomeSource': 'engine_default'}


def probe(engine: dict, environment: dict, policy: dict, *, entry: dict | None = None) -> dict:
    command = [sys.executable, str(Path(engine['engineRoot']) / 'scripts/mcp_server.py')]
    cwd = engine['engineRoot']
    if entry:
        command = [entry['command'], *entry['args']]
        environment = {**environment, **entry.get('env', {})}
        cwd = entry.get('cwd', cwd)
    requests = (
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'govel-bind', 'version': engine['version']}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'get_tool_help', 'arguments': {'operation': 'project.overview'}}},
    )
    try:
        result = subprocess.run(command, cwd=cwd,
            env={**os.environ, **environment, 'GOVEL_LOG_MODE': 'off', 'PYTHONDONTWRITEBYTECODE': '1'},
            input=''.join(json.dumps(row) + '\n' for row in requests), capture_output=True, text=True,
            timeout=policy['handshakeTimeoutSeconds'], check=False)
        if result.returncode:
            raise ValueError('server exit')
        replies = {}
        for line in result.stdout.splitlines():
            row = json.loads(line)
            if not isinstance(row, dict) or row.get('jsonrpc') != '2.0' or 'error' in row:
                raise ValueError('invalid response')
            if 'id' in row:
                if type(row['id']) is not int or row['id'] in replies or row['id'] not in {1, 2, 3}:
                    raise ValueError('unexpected response identity')
                replies[row['id']] = row['result']
        info = replies[1]['serverInfo']
        tools = replies[2]['tools']
        help_result = replies[3]
        structured = help_result.get('structuredContent')
        if structured is None:
            structured = json.loads(help_result['content'][0]['text'])
        if (info['name'] != 'govel' or info['version'] != engine['version']
                or not isinstance(tools, list) or not all(isinstance(row, dict) for row in tools)
                or not any(row.get('name') == 'get_tool_help' for row in tools)
                or help_result.get('isError') is True or not isinstance(structured, dict)
                or structured.get('ok') is not True):
            raise ValueError('invalid Govel proof')
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        raise BindError('MCP_HANDSHAKE_FAILED', '独立引擎进程未完成有效握手和帮助调用；未确认接入',
                        engineRoot=engine['engineRoot']) from exc
    return {'ok': True, 'serverVersion': info['version'], 'toolCount': len(tools),
            'verifiedCalls': ['initialize', 'tools/list', 'get_tool_help']}


@contextmanager
def installation_lock(state_root: Path):
    import fcntl
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state_root / 'install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise BindError('BIND_STATE_UNSAFE', '安装锁不是当前用户的独立普通文件')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BindError('BIND_INSTALL_BUSY', '另一接入安装正在运行；请稍后重试') from exc
        yield
    finally:
        os.close(fd)


def save_binding(state_root: Path, engine: dict, commit: str | None, data: dict) -> bool:
    value = {'schemaVersion': 1, **engine, **data, 'commit': commit}
    previous = read_binding(state_root)
    if previous == value:
        return False
    fd, temporary = tempfile.mkstemp(prefix='.binding-', dir=state_root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, state_root / 'binding.json')
        directory = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def run(action: str, *, host: str = 'plugin', engine_root: str | None = None,
        state_root: str | None = None, mcp_args: list[str] | None = None) -> dict:
    policy = read_policy()
    require_runtime(policy)
    state = state_path(state_root)

    def execute():
        if action == 'install':
            read_binding(state)
        engine, commit = locate_engine(engine_root, state, policy)
        if engine is None and action == 'install':
            engine, commit = install_engine(state, policy, engine_info)
        if engine is None:
            raise BindError('ENGINE_NOT_BOUND', '未找到完整 Govel 引擎；运行 install 或指定 --engine-root',
                            stateRoot=str(state))
        environment, data = data_environment(engine, state, host)
        if action == 'serve':
            arguments = list(mcp_args or [])
            if arguments[:1] == ['--']:
                arguments.pop(0)
            os.execve(sys.executable, [sys.executable, '-u', str(Path(engine['engineRoot']) / 'scripts/mcp_server.py'),
                                      *arguments], {**os.environ, **environment})
            raise AssertionError('execve returned')
        registration = inspect_registration(host, engine)
        if registration.get('entry'):
            environment = {}
            data = registered_data(engine, registration['entry'])
        handshake = probe(engine, environment, policy, entry=registration.get('entry'))
        if action == 'install':
            registration = install_registration(host, engine, environment, policy)
            # Verify the registration that actually survived the host writer.
            if registration.get('entry'):
                environment = {}
                data = registered_data(engine, registration['entry'])
                handshake = probe(engine, environment, policy, entry=registration['entry'])
        ready = registration['status'] in {'configured', 'managed_by_host_plugin'}
        report = {'ok': ready, 'code': 'GOVEL_BIND_READY' if ready else 'MCP_REGISTRATION_MISSING',
                  **engine, **data, 'stateRoot': str(state), 'commit': commit,
                  'registration': {k: v for k, v in registration.items() if k != 'entry'},
                  'processHandshake': handshake, 'hostConnection': 'not_observed',
                  'cliCommand': [sys.executable, str(Path(engine['engineRoot']) / 'scripts/novelctl.py')],
                  'nextAction': {'tool': 'get_tool_help', 'arguments': {'operation': 'project.overview', 'view': 'full'},
                                 'instruction': '在当前宿主会话调用该工具；若尚未加载，刷新插件或新建会话后重试。'},
                  'authorApprovalGranted': False}
        if action == 'install':
            report['bindingChanged'] = save_binding(state, engine, commit, data)
        return report

    if action == 'install':
        with installation_lock(state):
            return execute()
    return execute()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='接入 Govel 引擎；不创建小说、不授予作品修改批准。')
    parser.add_argument('action', choices=('check', 'install', 'serve'))
    parser.add_argument('--host', choices=('plugin', 'codex', 'claude', 'antigravity'), default='plugin')
    parser.add_argument('--engine-root')
    parser.add_argument('--state-root')
    parser.add_argument('--json', action='store_true', help='输出机器可读结果')
    args, remainder = parser.parse_known_args(argv)
    if remainder and args.action != 'serve':
        parser.error('unrecognized arguments: ' + ' '.join(remainder))
    try:
        report = run(args.action, host=args.host, engine_root=args.engine_root, state_root=args.state_root,
                     mcp_args=remainder)
    except BindError as exc:
        report = {'ok': False, 'code': exc.code, 'message': str(exc), 'details': exc.details,
                  'hostConnection': 'not_observed', 'authorApprovalGranted': False}
    except (OSError, ValueError) as exc:
        report = {'ok': False, 'code': 'GOVEL_BIND_FAILED', 'message': str(exc),
                  'hostConnection': 'not_observed', 'authorApprovalGranted': False}
    stream = sys.stderr if args.action == 'serve' else sys.stdout
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stream)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
