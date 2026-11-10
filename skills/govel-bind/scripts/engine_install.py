"""Download one pinned public Govel engine; no project or host configuration writes."""
from __future__ import annotations

import ctypes
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile


class BindError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details


def read_download(url: str, policy: dict) -> bytes:
    request = Request(url, headers={'User-Agent': 'govel-bind', 'Accept': 'application/vnd.github+json'})
    try:
        with urlopen(request, timeout=policy['downloadTimeoutSeconds']) as response:
            expected = urlparse(url)
            actual = urlparse(response.geturl())
            if actual.scheme != 'https' or actual.hostname != expected.hostname:
                raise BindError('ENGINE_DOWNLOAD_REDIRECT', '引擎下载跳转到未经允许的位置')
            payload = response.read(policy['downloadMaxBytes'] + 1)
    except (OSError, ValueError) as exc:
        raise BindError('ENGINE_DOWNLOAD_FAILED', '无法从官方仓库下载引擎；未切换绑定') from exc
    if len(payload) > policy['downloadMaxBytes']:
        raise BindError('ENGINE_DOWNLOAD_LIMIT', '引擎下载超过配置大小上限')
    return payload


def _public_member(name: str, info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '\\' in name or '\x00' in name
            or any(part in {'', '.', '..'} or ':' in part for part in name.rstrip('/').split('/'))):
        raise BindError('ENGINE_ARCHIVE_INVALID', '引擎包含不安全路径')
    kind = stat.S_IFMT(info.external_attr >> 16)
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 1:
        raise BindError('ENGINE_ARCHIVE_INVALID', '引擎包不能包含链接、特殊文件或加密成员')
    if ('.git' in path.parts or '.govel' in path.parts
            or path.name in {'provider.json', 'provider-config.json', '.env'}
            or (path.parts[0] == 'local' and name.rstrip('/') not in
                {'local', 'local/README.md', 'local/knowledge', 'local/knowledge/.gitkeep'})
            or name.startswith(('knowledge/assets/', 'knowledge/sources/private/',
                                'knowledge/.cache/', 'knowledge/.history/'))):
        raise BindError('ENGINE_ARCHIVE_PRIVATE_DATA', '引擎包含私人资料或项目状态路径')
    if name == 'local/knowledge/.gitkeep' and info.file_size:
        raise BindError('ENGINE_ARCHIVE_PRIVATE_DATA', '公开知识目录占位文件必须为空')


def extract_engine(payload: bytes, directory: Path, commit: str, policy: dict) -> None:
    prefix = 'govel-' + commit + '/'
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) > policy['archiveMaxFiles'] or sum(row.file_size for row in members) > policy['archiveMaxBytes']:
                raise BindError('ENGINE_ARCHIVE_LIMIT', '引擎包解压规模超过配置上限')
            seen = set()
            checked = []
            for row in members:
                if row.filename == prefix and row.is_dir():
                    continue
                if not row.filename.startswith(prefix):
                    raise BindError('ENGINE_ARCHIVE_INVALID', '引擎包不属于已解析的提交')
                name = row.filename[len(prefix):]
                _public_member(name, row)
                key = name.rstrip('/')
                if key in seen:
                    raise BindError('ENGINE_ARCHIVE_INVALID', '引擎包包含重复路径')
                seen.add(key)
                checked.append((name, row))
            for name, row in checked:
                target = directory / name
                if row.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(row) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1 << 20)
                    os.fchmod(output.fileno(), 0o700 if row.external_attr >> 16 & 0o111 else 0o600)
                    output.flush()
                    os.fsync(output.fileno())
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise BindError('ENGINE_ARCHIVE_INVALID', '引擎包无法完整安全地解压；未切换绑定') from exc


def publish_directory(source: Path, destination: Path) -> None:
    """Publish a complete cache directory without replacing an existing name."""
    if source.parent != destination.parent:
        raise ValueError('cache publication must stay in one directory')
    symbol, flag = ('renameatx_np', 4) if platform.system() == 'Darwin' else ('renameat2', 1)
    try:
        rename = getattr(ctypes.CDLL(None, use_errno=True), symbol)
    except AttributeError as exc:
        raise BindError('ENGINE_PUBLICATION_UNAVAILABLE', '运行时缺少独占目录发布能力') from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    descriptor = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if rename(descriptor, os.fsencode(source.name), descriptor, os.fsencode(destination.name), flag):
            value = ctypes.get_errno()
            raise OSError(value, os.strerror(value), str(destination))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_directories(root: Path) -> None:
    """Make every extracted directory entry durable before exposing the engine."""
    directories = [root, *[path for path in root.rglob('*') if path.is_dir()]]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def install_engine(state_root: Path, policy: dict, validate) -> tuple[dict, str]:
    repository = policy['repository']
    if repository != 'wuren194/govel' or not re.fullmatch(r'[A-Za-z0-9._/-]+', policy['ref']):
        raise BindError('BIND_POLICY_INVALID', '引导器仅允许官方 Govel 仓库')
    try:
        reply = json.loads(read_download(f'https://api.github.com/repos/{repository}/commits/{policy["ref"]}', policy))
        commit = reply['sha']
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise ValueError('invalid commit')
    except (KeyError, TypeError, ValueError) as exc:
        raise BindError('ENGINE_COMMIT_INVALID', '官方仓库未返回可固定的提交身份') from exc
    engines = state_root / 'engines'
    if engines.is_symlink():
        raise BindError('BIND_STATE_UNSAFE', '引擎缓存目录不能是符号链接')
    engines.mkdir(mode=0o700, exist_ok=True)
    destination = engines / commit
    if destination.is_symlink():
        raise BindError('BIND_STATE_UNSAFE', '已缓存引擎不能是符号链接')
    if destination.exists():
        return validate(destination, policy), commit
    payload = read_download(f'https://codeload.github.com/{repository}/zip/{commit}', policy)
    staging = Path(tempfile.mkdtemp(prefix='.install-', dir=engines))
    try:
        extract_engine(payload, staging, commit, policy)
        validate(staging, policy)
        sync_directories(staging)
        publish_directory(staging, destination)
        return validate(destination, policy), commit
    finally:
        if staging.exists():
            shutil.rmtree(staging)
