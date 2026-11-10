"""Narrow first-time host registration; existing entries belong to the user."""
from __future__ import annotations

import importlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types

from engine_install import BindError


def configuration_path(host: str) -> Path:
    if host == 'codex':
        return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
    if host == 'claude':
        if os.environ.get('CLAUDE_CONFIG_DIR'):
            return Path(os.environ['CLAUDE_CONFIG_DIR']) / '.claude.json'
        return Path.home() / '.claude.json'
    return Path.home() / '.gemini/config/mcp_config.json'


def decode_configuration(host: str, raw: bytes | None) -> dict:
    if raw is None:
        return {}
    try:
        text = raw.decode('utf-8')
        if host == 'codex':
            import tomllib
            data = tomllib.loads(text)
        else:
            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate configuration key')
                    result[key] = value
                return result
            data = json.loads(text, object_pairs_hook=unique_pairs)
        if not isinstance(data, dict):
            raise ValueError('configuration must be an object')
        return data
    except (OSError, ValueError) as exc:
        raise BindError('MCP_CONFIG_INVALID', '宿主配置无法安全读取；未修改配置') from exc


def _read_configuration(host: str, path: Path) -> dict:
    if path.is_symlink():
        raise BindError('MCP_CONFIG_UNSAFE', '宿主配置是符号链接；未修改配置', path=str(path))
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    except OSError as exc:
        raise BindError('MCP_CONFIG_INVALID', '宿主配置无法安全读取；未修改配置', path=str(path)) from exc
    return decode_configuration(host, raw)


def selected_entry(host: str, data: dict) -> dict:
    collection = 'mcp_servers' if host == 'codex' else 'mcpServers'
    entries = data.get(collection, {})
    if not isinstance(entries, dict):
        raise BindError('MCP_CONFIG_INVALID', '宿主 MCP 配置不是对象；未修改配置')
    result = {'present': 'govel' in entries, 'entry': entries.get('govel'),
              'scope': 'global' if host == 'codex' else 'user', 'path': None}
    if host == 'claude':
        projects = data.get('projects', {})
        if not isinstance(projects, dict):
            raise BindError('MCP_CONFIG_INVALID', 'Claude 项目配置不是对象；未修改配置')
        cwd = Path.cwd().resolve()
        matching = []
        for location, project in projects.items():
            if not isinstance(location, str) or not Path(location).is_absolute():
                continue
            root = Path(location).resolve()
            if root == cwd or root in cwd.parents:
                matching.append((root, project))
        matching.sort(key=lambda item: len(item[0].parts), reverse=True)
        if matching:
            local = matching[0][1]
            if not isinstance(local, dict) or not isinstance(local.get('mcpServers', {}), dict):
                raise BindError('MCP_CONFIG_INVALID', '当前 Claude 项目配置无法安全读取')
            if 'govel' in local.get('mcpServers', {}):
                return {'present': True, 'entry': local['mcpServers']['govel'], 'scope': 'local', 'path': None}
        git_root = next((folder for folder in (cwd, *cwd.parents) if (folder / '.git').exists()), None)
        roots = [root for root, _ in matching[:1]] + ([git_root] if git_root else [])
        project_root = max(roots, key=lambda root: len(root.parts)) if roots else cwd
        project_path = project_root / '.mcp.json'
        project_data = _read_configuration('claude', project_path)
        project_entries = project_data.get('mcpServers', {})
        if not isinstance(project_entries, dict):
            raise BindError('MCP_CONFIG_INVALID', '当前项目 .mcp.json 不是合法 MCP 配置')
        if 'govel' in project_entries:
            return {'present': True, 'entry': project_entries['govel'], 'scope': 'project', 'path': str(project_path)}
    return result


def inspect_registration(host: str, engine: dict) -> dict:
    if host == 'plugin':
        return {'status': 'managed_by_host_plugin', 'changed': False, 'scope': 'plugin',
                'hostInstallation': 'not_observed', 'entry': None}
    path = configuration_path(host)
    data = _read_configuration(host, path)
    selected = selected_entry(host, data)
    entry = selected['entry']
    scope = {'scope': selected['scope'], 'scopeCoverage': 'global_configuration_only' if host == 'codex'
             else 'local_project_user' if host == 'claude' else 'global_configuration_only'}
    if not selected['present']:
        return {'status': 'missing', 'changed': False, 'configPath': str(path), 'entry': None, **scope}
    root = Path(engine['engineRoot'])
    expected = root / 'scripts/mcp_server.py'
    valid = isinstance(entry, dict)
    if valid:
        arguments = entry.get('args', [])
        arguments = arguments[1:] if isinstance(arguments, list) and arguments and arguments[0] == '-u' else arguments
        environment = entry.get('env', {})
        command = entry.get('command', '')
        valid = (entry.get('type', 'stdio') == 'stdio'
                 and all(type(entry[key]) is bool for key in ('enabled', 'disabled') if key in entry)
                 and entry.get('enabled', True) is not False
                 and entry.get('disabled', False) is not True and 'url' not in entry
                 and isinstance(command, str) and bool(re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(command).name))
                 and isinstance(arguments, list) and bool(arguments)
                 and all(isinstance(value, str) for value in arguments)
                 and Path(arguments[0]).is_absolute() and Path(arguments[0]).resolve() == expected.resolve()
                 and isinstance(environment, dict)
                 and all(isinstance(k, str) and isinstance(v, str) and '\0' not in k + v for k, v in environment.items()))
    if not valid:
        raise BindError('MCP_REGISTRATION_CONFLICT', '已有 govel 注册与本次引擎冲突；保留原条目和环境设置',
                        host=host, configPath=selected['path'] or str(path), scope=selected['scope'])
    return {'status': 'configured', 'changed': False, 'configPath': selected['path'] or str(path),
            'entry': entry, **scope}


def registration_command(host: str, engine: dict, environment: dict) -> list[str]:
    executable = shutil.which('codex' if host == 'codex' else 'claude')
    if not executable:
        raise BindError('HOST_CLI_UNAVAILABLE', f'未找到 {host} 命令；先安装或启用该宿主 CLI')
    server = str(Path(engine['engineRoot']) / 'scripts/mcp_server.py')
    if host == 'codex':
        result = [executable, 'mcp', 'add', 'govel']
        for key, value in sorted(environment.items()):
            result += ['--env', key + '=' + value]
        return [*result, '--', sys.executable, server]
    entry = {'type': 'stdio', 'command': sys.executable, 'args': [server]}
    if environment:
        entry['env'] = environment
    return [executable, 'mcp', 'add-json', '-s', 'user', 'govel', json.dumps(entry, ensure_ascii=False)]


def requested_entry_matches(entry, engine, environment):
    desired = {'command': sys.executable, 'args': [str(Path(engine['engineRoot']) / 'scripts/mcp_server.py')],
               'env': environment, 'type': 'stdio'}
    return isinstance(entry, dict) and {**{'env': {}, 'type': 'stdio'}, **entry} == desired


def publication_module(engine: dict):
    """Load the selected engine's publication API without reusing another checkout."""
    root = Path(engine['engineRoot'])
    package = '_govel_binding_' + hashlib.sha256(os.fsencode(root)).hexdigest()
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(root / 'scripts/core')]
        sys.modules[package] = module
    try:
        module = importlib.import_module(package + '.antigravity_registration')
        if not callable(getattr(module, 'publish_host_configuration', None)):
            raise ImportError('missing publication API')
        return module
    except (ImportError, AttributeError, SyntaxError) as exc:
        raise BindError('ENGINE_UPDATE_REQUIRED', '所选引擎缺少当前接入发布接口；请更新完整 Govel 引擎') from exc


def _generated_entry(host: str, engine: dict, environment: dict, policy: dict) -> tuple[dict, bytes]:
    """Ask the official CLI to generate only a new entry in an empty private scope."""
    command = registration_command(host, engine, environment)
    with tempfile.TemporaryDirectory(prefix='govel-host-candidate-') as temporary:
        directory = Path(temporary).resolve()
        variable, filename = ('CODEX_HOME', 'config.toml') if host == 'codex' else ('CLAUDE_CONFIG_DIR', '.claude.json')
        # No original configuration or credentials go to the candidate process.
        allowed = {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'TMPDIR', 'TMP', 'TEMP', 'TERM'}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env[variable] = str(directory)
        env['DISABLE_TELEMETRY'] = '1'
        env['DISABLE_ERROR_REPORTING'] = '1'
        try:
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True,
                                    timeout=policy['handshakeTimeoutSeconds'], check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BindError('MCP_REGISTRATION_FAILED', '宿主未能在隔离目录生成注册候选；未修改原配置') from exc
        if result.returncode:
            raise BindError('MCP_REGISTRATION_FAILED', '宿主拒绝生成注册候选；未修改原配置', host=host,
                            exitCode=result.returncode)
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise BindError('MCP_REGISTRATION_UNVERIFIED', '宿主未在指定隔离目录生成配置；未修改原配置')
        raw = path.read_bytes()
        generated = decode_configuration(host, raw)
        collection = 'mcp_servers' if host == 'codex' else 'mcpServers'
        entries = generated.get(collection)
        if not isinstance(entries, dict) or set(entries) != {'govel'}:
            raise BindError('MCP_CANDIDATE_INVALID', '宿主候选没有唯一 govel 注册')
        entry = entries['govel']
        if not requested_entry_matches(entry, engine, environment):
            raise BindError('MCP_CANDIDATE_INVALID', '宿主候选改变了本次接入参数；未修改原配置')
        if host == 'codex' and set(generated) != {collection}:
            raise BindError('MCP_CANDIDATE_INVALID', '宿主候选带有接入以外的配置；未修改原配置')
        return entry, raw


def install_registration(host: str, engine: dict, environment: dict, policy: dict) -> dict:
    current = inspect_registration(host, engine)
    if current['status'] != 'missing':
        return current
    module = publication_module(engine)
    path = module._configuration_path(Path(current['configPath']))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if host == 'antigravity':
        root = Path(engine['engineRoot'])
        try:
            result = module.install_registration(path, root / 'scripts/mcp_server.py',
                                                 environment=environment, only_if_missing=True)
        except (OSError, ValueError) as exc:
            raise BindError(getattr(exc, 'code', 'MCP_REGISTRATION_FAILED'), str(exc)) from exc
        observed = inspect_registration(host, engine)
        if not requested_entry_matches(observed.get('entry'), engine, environment):
            raise BindError('MCP_CONFIG_CHANGED', '发布后 govel 注册已变化；未切换绑定')
        return {**observed, 'changed': result['changed']}

    def prepare(snapshot):
        data = decode_configuration(host, snapshot['raw'])
        if selected_entry(host, data)['present']:
            raise BindError('MCP_REGISTRATION_CONFLICT', '注册检查后出现新的 govel 条目；未替换它')
        entry, generated_raw = _generated_entry(host, engine, environment, policy)
        collection = 'mcp_servers' if host == 'codex' else 'mcpServers'
        expected = {**data, collection: {**data.get(collection, {}), 'govel': entry}}
        if host == 'codex':
            candidate = (snapshot['raw'] or b'') + b'\n' + generated_raw
        else:
            candidate = (json.dumps(expected, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
        if decode_configuration(host, candidate) != expected:
            raise BindError('MCP_CANDIDATE_INVALID', '候选改变了其他宿主设置；未修改原配置')
        return candidate

    try:
        result = module.publish_host_configuration(path, prepare)
    except (OSError, ValueError) as exc:
        raise BindError(getattr(exc, 'code', 'MCP_REGISTRATION_FAILED'), str(exc)) from exc
    observed = inspect_registration(host, engine)
    if (observed['status'] != 'configured' or (host == 'claude' and observed['scope'] != 'user')
            or not requested_entry_matches(observed.get('entry'), engine, environment)):
        raise BindError('MCP_REGISTRATION_UNVERIFIED', '宿主命令退出成功，但未找到对应注册')
    return {**observed, 'changed': result['changed']}
