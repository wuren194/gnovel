"""Safe edits to Antigravity's one documented global MCP configuration.

No client session is inspected or started here. Static configuration health is
not evidence that a running Antigravity process has loaded the registration.
"""
from __future__ import annotations

from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

from .mcp_options import startup_arguments_valid


class RegistrationError(ValueError):
    def __init__(self, code: str, message: str, *, backup_path=None):
        super().__init__(message)
        self.code = code
        self.backup_path = str(backup_path) if backup_path is not None else None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink)


def _configuration_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    # /var is a normal macOS system alias. Inspect application-owned path
    # components before resolving that alias, so a symlinked config or .gemini
    # directory cannot redirect this installer to an unrelated file tree.
    parts = path.parts
    first = parts.index('.gemini') if '.gemini' in parts else len(parts) - 2
    for index in range(max(first, 1), len(parts)):
        current = Path(*parts[:index + 1])
        if current.is_symlink():
            raise RegistrationError('MCP_CONFIG_SYMLINK', 'MCP 配置及其应用目录不得是符号链接')
    return path.parent.resolve() / path.name


def _read_file(path: Path) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'data': {}, 'raw': None, 'identity': None, 'mode': 0o600}
    except OSError as exc:
        raise RegistrationError('MCP_CONFIG_UNREADABLE', 'MCP 配置无法读取') from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
            raise RegistrationError('MCP_CONFIG_UNSAFE', 'MCP 配置须为当前用户拥有的普通文件，不能是硬链接')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read()
        if (_identity(before) != _identity(os.fstat(descriptor))
                or _identity(before) != _identity(path.stat(follow_symlinks=False))):
            raise RegistrationError('MCP_CONFIG_CHANGED', '读取时 MCP 配置发生变化，请重新检查')
    finally:
        os.close(descriptor)
    return {'raw': raw, 'identity': _identity(before), 'mode': stat.S_IMODE(before.st_mode)}


def read_configuration(path: Path) -> dict:
    """Read a validated JSON object plus the exact bytes used for compare/write."""
    path = _configuration_path(path)
    snapshot = _read_file(path)
    if snapshot['raw'] is None:
        return snapshot
    raw = snapshot['raw']
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate JSON key')
                result[key] = value
            return result

        def constant(value):
            raise ValueError('non-JSON numeric constant')

        def number(value):
            result = float(value)
            if not math.isfinite(result):
                raise ValueError('non-finite JSON number')
            return result

        data = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except (ValueError, UnicodeError) as exc:
        raise RegistrationError('MCP_CONFIG_INVALID_JSON', 'MCP 配置不是合法 JSON；保留原文件') from exc
    if not isinstance(data, dict):
        raise RegistrationError('MCP_CONFIG_INVALID', 'MCP 配置顶层必须是对象；保留原文件')
    if 'mcpServers' in data and not isinstance(data['mcpServers'], dict):
        raise RegistrationError('MCP_SERVERS_INVALID', 'mcpServers 必须是对象；保留原文件')
    return {**snapshot, 'data': data}


def _python_command(command, environment: dict) -> str | None:
    if not isinstance(command, str) or not command or '\0' in command:
        return None
    # An explicit server PATH replaces the installer's PATH at subprocess
    # startup. An absolute interpreter remains independent of that override.
    resolved = shutil.which(command, path=environment.get('PATH', os.environ.get('PATH', os.defpath)))
    if resolved is None or not Path(resolved).is_file():
        return None
    if (not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(command).name)
            and Path(resolved).resolve() != Path(sys.executable).resolve()):
        return None
    return resolved


def inspect_entry(entry, script: Path) -> dict:
    problems = []

    def problem(code, message):
        problems.append({'code': code, 'message': message})

    if entry is None:
        problem('MCP_REGISTRATION_MISSING', '官方全局 MCP 配置缺少 govel 注册')
        return {'configHealthy': False, 'problems': problems}
    if not isinstance(entry, dict):
        problem('MCP_ENTRY_INVALID', 'govel 注册必须是对象')
        return {'configHealthy': False, 'problems': problems}
    for key, inactive in (('disabled', True), ('enabled', False)):
        if key in entry and type(entry[key]) is not bool:
            problem('MCP_ENABLED_INVALID', 'govel 启用字段必须是布尔值')
        elif entry.get(key) is inactive:
            problem('MCP_REGISTRATION_DISABLED', 'govel 注册已禁用')
    if entry.get('type', 'stdio') != 'stdio' or 'url' in entry or 'serverUrl' in entry:
        problem('MCP_TRANSPORT_CONFLICT', 'govel 应使用本地 stdio，不能同时带远程 URL')
    environment = entry.get('env', {})
    environment_valid = (isinstance(environment, dict)
                         and all(isinstance(key, str) and key and '=' not in key and '\0' not in key
                                 and isinstance(value, str) and '\0' not in value for key, value in environment.items()))
    command = _python_command(entry.get('command'), environment if environment_valid else {})
    if command is None:
        problem('MCP_COMMAND_INVALID', 'govel command 不是当前环境可执行的 Python')
    args = entry.get('args')
    valid_args = isinstance(args, list) and bool(args) and all(isinstance(arg, str) and '\0' not in arg for arg in args)
    if valid_args:
        try:
            valid_args = Path(args[0]).is_absolute() and Path(args[0]).resolve() == script.resolve()
        except (OSError, ValueError, RuntimeError):
            valid_args = False
    if not valid_args:
        problem('MCP_SCRIPT_MISMATCH', 'govel args 的首项必须是当前工具箱 MCP 入口的绝对路径')
    elif not startup_arguments_valid(args[1:]):
        problem('MCP_ARGUMENTS_INVALID', 'govel 的入口参数无法启动服务；请移除无效选项或 help 参数')
    if not script.is_file():
        problem('MCP_SCRIPT_MISSING', '当前工具箱缺少 scripts/mcp_server.py')
    if not environment_valid:
        problem('MCP_ENV_INVALID', 'govel env 必须是合法字符串键值对象')
    if 'cwd' in entry and (not isinstance(entry['cwd'], str) or '\0' in entry['cwd'] or not Path(entry['cwd']).is_absolute()
                           or not Path(entry['cwd']).is_dir()):
        problem('MCP_CWD_INVALID', 'govel cwd 必须是存在的绝对目录')
    return {'configHealthy': not problems, 'problems': problems,
            'resolvedCommand': command, 'script': str(script)}


def _desired_configuration(snapshot: dict, script: Path) -> dict:
    data = deepcopy(snapshot['data'])
    entries = data.setdefault('mcpServers', {})
    entry = entries.get('govel')
    if 'govel' in entries and not isinstance(entry, dict):
        raise RegistrationError('MCP_ENTRY_INVALID', 'govel 注册不是对象，无法安全合并；保留原文件')
    entry = deepcopy(entry or {})
    # Environment and unknown options are user-owned. Malformed environment
    # data must not be silently discarded while repairing the launch command.
    environment = entry.get('env', {})
    if (not isinstance(environment, dict)
            or any(not isinstance(key, str) or not key or '=' in key or '\0' in key
                   or not isinstance(value, str) or '\0' in value for key, value in environment.items())):
        raise RegistrationError('MCP_ENV_INVALID', 'govel env 无法安全合并；保留原文件')
    args = entry.get('args')
    tail = (args[1:] if isinstance(args, list) and args and all(isinstance(arg, str) and '\0' not in arg for arg in args)
            and args[0].endswith('.py') else [])
    if not startup_arguments_valid(tail):
        tail = []
    entry.update(command=sys.executable, args=[str(script), *tail])
    if 'type' in entry:
        entry['type'] = 'stdio'
    for key in ('url', 'serverUrl'):
        entry.pop(key, None)
    if 'disabled' in entry:
        entry['disabled'] = False
    if 'enabled' in entry:
        entry['enabled'] = True
    if 'cwd' in entry and (not isinstance(entry['cwd'], str) or '\0' in entry['cwd'] or not Path(entry['cwd']).is_absolute()
                           or not Path(entry['cwd']).is_dir()):
        entry.pop('cwd')
    entries['govel'] = entry
    inspected = inspect_entry(entry, script)
    if not inspected['configHealthy']:
        raise RegistrationError('MCP_REPAIR_INVALID', '无法生成有效的本地 MCP 注册')
    return data


def _write_private(path: Path, raw: bytes):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _same_configuration(path: Path, expected: dict):
    current = read_configuration(path)
    if current['identity'] != expected['identity'] or current['raw'] != expected['raw']:
        raise RegistrationError('MCP_CONFIG_CHANGED', 'MCP 配置已被其他进程修改，本次安装不覆盖它')


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _pending_path(path):
    return path.parent / '.govel-mcp-install-pending.json'


def pending_registration(path: Path) -> dict | None:
    """Return only validated recovery locations; never create or restore files."""
    path = _configuration_path(path)
    record = _read_file(_pending_path(path))
    if record['raw'] is None:
        return None
    try:
        payload = json.loads(record['raw'])
        keys = {'schemaVersion', 'token', 'originalSha256', 'installedSha256', 'originalMode'}
        if (not isinstance(payload, dict) or set(payload) != keys or type(payload['schemaVersion']) is not int
                or payload['schemaVersion'] != 1 or not isinstance(payload['token'], str)
                or not re.fullmatch('[0-9a-f]{32}', payload['token'])
                or type(payload['originalMode']) is not int or not 0 <= payload['originalMode'] <= 0o7777):
            raise ValueError('invalid recovery shape')
        for key in ('originalSha256', 'installedSha256'):
            value = payload[key]
            if value is None and key == 'originalSha256':
                continue
            if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
                raise ValueError('invalid recovery digest')
    except (ValueError, TypeError, KeyError) as exc:
        raise RegistrationError('MCP_RECOVERY_INVALID', 'MCP 安装恢复标记损坏；保留配置和备份') from exc
    return {**payload,
            'backupPath': str(path.parent / f'.{path.name}.govel-backup-{payload["token"]}'),
            'stagedPath': str(path.parent / f'.govel-mcp-staged-{payload["token"]}')}


def _restore_backup(path: Path, backup: Path, mode: int) -> bool:
    """Copy the actual displaced version back only if nobody created a target."""
    saved = _read_file(backup)
    if saved['raw'] is None:
        raise RegistrationError('MCP_RECOVERY_MISSING', '被移走的 MCP 配置备份不可读', backup_path=backup)
    temporary = path.parent / ('.govel-mcp-restore-' + uuid.uuid4().hex)
    try:
        _write_private(temporary, saved['raw'])
        temporary.chmod(mode)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return False
        _sync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _clear_pending(path, pending):
    Path(pending['stagedPath']).unlink(missing_ok=True)
    _pending_path(path).unlink()
    _sync_directory(path.parent)


def _recover_pending(path: Path) -> dict | None:
    """Recover a killed publication without replacing a concurrent new config."""
    pending = pending_registration(path)
    if pending is None:
        return None
    backup = Path(pending['backupPath'])
    staged = Path(pending['stagedPath'])
    try:
        published, candidate = path.lstat(), staged.lstat()
    except FileNotFoundError:
        pass
    else:
        if (stat.S_ISREG(published.st_mode) and stat.S_ISREG(candidate.st_mode)
                and published.st_uid == candidate.st_uid == os.getuid()
                and published.st_dev == candidate.st_dev and published.st_ino == candidate.st_ino
                and published.st_nlink == candidate.st_nlink == 2):
            # A kill after the no-replace link but before staged unlink leaves
            # exactly these two owned names for the newly published inode.
            staged.unlink()
            _sync_directory(path.parent)
    saved = _read_file(backup)
    if saved['raw'] is not None:
        backup.chmod(0o600)
    current = _read_file(path)
    if current['raw'] is None and saved['raw'] is not None:
        if not _restore_backup(path, backup, pending['originalMode']):
            raise RegistrationError('MCP_RECOVERY_CONFLICT', '恢复时出现新的 MCP 配置，当前文件和实际备份均保留', backup_path=backup)
        _clear_pending(path, pending)
        return {'action': 'restored_actual_backup', 'backupPath': str(backup)}
    if current['raw'] is None:
        if pending['originalSha256'] is not None:
            raise RegistrationError('MCP_RECOVERY_MISSING', '原 MCP 配置及备份均缺失，保留恢复标记', backup_path=backup)
        _clear_pending(path, pending)
        return {'action': 'cleared_unpublished_new_config', 'backupPath': None}
    current_digest = hashlib.sha256(current['raw']).hexdigest()
    if (saved['raw'] is not None and hashlib.sha256(saved['raw']).hexdigest() != pending['originalSha256']
            and current['raw'] != saved['raw']):
        _clear_pending(path, pending)
        raise RegistrationError('MCP_RECOVERY_CONFLICT', '实际备份含并发更新；当前配置和备份均保留，不将安装视作成功',
                                backup_path=backup)
    expected = {pending['originalSha256'], pending['installedSha256']}
    if saved['raw'] is not None:
        expected.add(hashlib.sha256(saved['raw']).hexdigest())
    if current_digest not in expected:
        # Recovery never silently adopts a competing update. Preserve both
        # versions, clear the abandoned transaction, and let a fresh invocation
        # inspect and merge that current configuration after an explicit error.
        _clear_pending(path, pending)
        raise RegistrationError('MCP_RECOVERY_CONFLICT', 'MCP 配置已由其他进程修改，当前文件和实际备份均保留',
                                backup_path=backup if saved['raw'] is not None else None)
    _clear_pending(path, pending)
    return {'action': 'cleared_completed_or_aborted_publication',
            'backupPath': str(backup) if saved['raw'] is not None else None}


def _publish_configuration(path: Path, snapshot: dict, raw: bytes, token: str) -> Path | None:
    """Preserve the inode actually displaced, then publish without replacing.

    The temporary absence is intentional: os.replace cannot conditionally
    replace a particular file version. A durable marker allows the next install
    to restore the actual displaced version after a kill in this short window.
    """
    staged = path.parent / f'.govel-mcp-staged-{token}'
    backup = path.parent / f'.{path.name}.govel-backup-{token}'
    pending = {'schemaVersion': 1, 'token': token,
               'originalSha256': hashlib.sha256(snapshot['raw']).hexdigest() if snapshot['raw'] is not None else None,
               'installedSha256': hashlib.sha256(raw).hexdigest(), 'originalMode': snapshot['mode']}
    _write_private(staged, raw)
    staged.chmod(snapshot['mode'])
    _sync_directory(path.parent)
    try:
        _same_configuration(path, snapshot)
        _write_private(_pending_path(path), json.dumps(pending, allow_nan=False).encode('utf-8'))
        _sync_directory(path.parent)
        if snapshot['raw'] is not None:
            # This moves the *actual* current file, not a copy of an earlier
            # read. A concurrent replacement immediately before rename is
            # therefore retained in backup and cannot be lost.
            os.rename(path, backup)
            _sync_directory(path.parent)
            actual = _read_file(backup)
            backup.chmod(0o600)
            if actual['raw'] != snapshot['raw'] or actual['identity'][:2] != snapshot['identity'][:2]:
                _restore_backup(path, backup, actual['mode'])
                raise RegistrationError('MCP_CONFIG_CHANGED', '发布前 MCP 配置发生变化；实际被移走的版本已保留', backup_path=backup)
        try:
            os.link(staged, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise RegistrationError('MCP_CONFIG_CHANGED', '发布时出现新的 MCP 配置，不覆盖它；实际备份保留',
                                    backup_path=backup if backup.exists() else None) from exc
        staged.unlink()
        _sync_directory(path.parent)
        # A process holding the old descriptor may have written through it
        # after rename. Its bytes remain in backup and must not be reported as
        # an unchanged successful merge.
        if snapshot['raw'] is not None and _read_file(backup)['raw'] != snapshot['raw']:
            raise RegistrationError('MCP_CONFIG_CHANGED', '旧文件描述符继续修改了实际备份；保留两份内容供核对', backup_path=backup)
        _clear_pending(path, {**pending, 'stagedPath': str(staged)})
        return backup if snapshot['raw'] is not None else None
    except Exception:
        if _pending_path(path).exists():
            try:
                _recover_pending(path)
            except (RegistrationError, OSError):
                # The actual backup and pending marker remain available when
                # restoration cannot safely finish. The original error wins.
                pass
        raise
    finally:
        if not _pending_path(path).exists():
            staged.unlink(missing_ok=True)


def install_registration(path: Path, script: Path) -> dict:
    """Preserve other entries; publish only after an unchanged-byte check."""
    path = _configuration_path(path)
    if not path.parent.is_dir():
        raise RegistrationError('MCP_CONFIG_PARENT_MISSING', 'Antigravity 官方配置目录不存在')
    lock_path = path.parent / '.govel-mcp-install.lock'
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise RegistrationError('MCP_INSTALL_LOCK_UNSAFE', 'MCP 安装锁不是当前用户的独立普通文件')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RegistrationError('MCP_INSTALL_BUSY', '另一安装进程正在修改 MCP 配置') from exc
        recovery = _recover_pending(path)
        snapshot = read_configuration(path)
        desired = _desired_configuration(snapshot, script)
        if (json.dumps(desired, sort_keys=True, allow_nan=False) == json.dumps(snapshot['data'], sort_keys=True, allow_nan=False)
                and snapshot['raw'] is not None):
            return {'changed': False, 'backupPath': None, 'changedFields': [], 'recovery': recovery}
        previous = snapshot['data'].get('mcpServers', {}).get('govel', {})
        current = desired['mcpServers']['govel']
        absent = object()
        changed_fields = sorted(key for key in previous.keys() | current.keys()
                                if type(previous.get(key, absent)) is not type(current.get(key, absent))
                                or previous.get(key, absent) != current.get(key, absent))
        raw = (json.dumps(desired, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        backup = _publish_configuration(path, snapshot, raw, uuid.uuid4().hex)
        return {'changed': True, 'backupPath': str(backup) if backup else None,
                'changedFields': changed_fields, 'recovery': recovery}
    finally:
        os.close(descriptor)
