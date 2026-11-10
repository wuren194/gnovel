#!/usr/bin/env python3
"""Build a self-contained public plugin from the current Git worktree.

The archive contains the runtime and plugin entrypoints, not developer tests or
personal data. Git supplies candidates; a runtime allowlist and the public
release checks decide which of those candidates can enter the archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import zipfile

try:
    from scripts.runtime_requirements import require_supported_platform, require_supported_python
except ImportError:
    from runtime_requirements import require_supported_platform, require_supported_python

require_supported_python()
require_supported_platform()

try:
    from scripts.check_release_contents import check_paths, check_zip
    from scripts.core.file_preservation import exclusive_move, observation
    from scripts.core.local_layout import PUBLIC_LOCAL_FILES, local_content_issue
    from scripts.package_toolbox import _unsafe
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.check_release_contents import check_paths, check_zip
    from scripts.core.file_preservation import exclusive_move, observation
    from scripts.core.local_layout import PUBLIC_LOCAL_FILES, local_content_issue
    from scripts.package_toolbox import _unsafe


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    'AGENTS.md', 'CLA.md', 'CONTRIBUTING.md', 'LICENSE', 'README.md', 'SECURITY.md',
    'SKILL.md', 'THIRD_PARTY_NOTICES.md', 'requirements.txt', 'toolbox.manifest.json',
    'plugin.json', 'mcp.json', 'mcp_config.json',
    '.agents/skills.json', '.claude/SKILL.md',
}
RUNTIME_DIRECTORIES = {
    'scripts', 'shared', 'skills', 'commands', 'docs', 'LICENSES', 'dashboard',
    '.claude-plugin', '.codex-plugin', '.antigravity-plugin',
}
KNOWLEDGE_FILES = {'knowledge/README.md', 'knowledge/schema.json', 'knowledge/example.json'}
REQUIRED_FILES = {
    'LICENSE', 'THIRD_PARTY_NOTICES.md', 'SKILL.md', 'toolbox.manifest.json',
    'plugin.json', 'mcp.json', 'mcp_config.json',
    '.claude-plugin/plugin.json', '.claude-plugin/marketplace.json',
    '.antigravity-plugin/plugin.json', '.agents/plugins/marketplace.json',
    'skills/govel-bind/SKILL.md', 'skills/govel-bind/binding.json',
    'skills/govel-bind/scripts/bind.py', 'skills/govel/SKILL.md', 'commands/init.md',
    'scripts/mcp_server.py', 'scripts/novelctl.py', 'scripts/runtime_requirements.py',
    'scripts/core/project.py', *KNOWLEDGE_FILES, *PUBLIC_LOCAL_FILES,
    '.agents/skills.json', '.agents/skills/govel/SKILL.md', '.claude/SKILL.md',
}


class PluginPackageError(ValueError):
    def __init__(self, code: str, message: str, *, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _read(root: Path, relative: str):
    try:
        return observation(root / relative)
    except (OSError, ValueError) as exc:
        raise PluginPackageError('PLUGIN_SOURCE_UNSAFE', 'plugin input must be a readable ordinary file',
                                 details={'path': relative}) from exc


def _json(data: bytes, relative: str) -> dict:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise PluginPackageError('PLUGIN_MANIFEST_INVALID', 'plugin metadata is not valid JSON',
                                 details={'path': relative}) from exc
    if not isinstance(value, dict):
        raise PluginPackageError('PLUGIN_MANIFEST_INVALID', 'plugin metadata must be an object',
                                 details={'path': relative})
    return value


def _candidates(root: Path) -> list[str]:
    try:
        result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                cwd=root, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PluginPackageError('PLUGIN_SOURCE_NOT_WORKTREE', 'package source must be a readable Git worktree') from exc
    return sorted({os.fsdecode(name) for name in result.stdout.split(b'\0') if name})


def _runtime_path(relative: str, skills: set[str]) -> bool:
    path = PurePosixPath(relative)
    if (path.is_absolute() or path.as_posix() != relative or '..' in path.parts
            or '\\' in relative or '\0' in relative or _unsafe(relative)
            or set(path.parts) & {'.govel', '.cache', '.history', 'node_modules'}
            or path.name.casefold().startswith('.env.')):
        return False
    # Existing workspace wrappers are also consumed by the public doctor. Keep
    # those generated instructions, but no client settings or permission files.
    return (relative in ROOT_FILES | KNOWLEDGE_FILES | PUBLIC_LOCAL_FILES
            or relative == '.agents/plugins/marketplace.json'
            or relative.startswith(('.agents/skills/', '.claude/agents/', '.codex/agents/'))
            or path.parts[0] in RUNTIME_DIRECTORIES | skills)


def _validate_metadata(contents: dict[str, bytes], manifest: dict) -> None:
    required = REQUIRED_FILES | {f'{skill}/SKILL.md' for skill in manifest['skills']}
    for skill in manifest['skills']:
        required.update({f'.agents/skills/{skill}/SKILL.md', f'.claude/agents/{skill}.md',
                         f'.codex/agents/{skill}.toml'})
    protocols = manifest.get('sharedProtocols', {})
    for key in ('workflowPolicy', 'settingsSchema', 'sourceModel', 'toolRegistry'):
        if isinstance(protocols.get(key), str):
            required.add(protocols[key])
    contracts = protocols.get('contractsList', [])
    if not isinstance(contracts, list) or not all(isinstance(name, str) for name in contracts):
        raise PluginPackageError('PLUGIN_MANIFEST_INVALID', 'contract inventory is invalid')
    required.update(f"{protocols.get('contracts', 'shared/contracts')}/{name}" for name in contracts)
    missing = sorted(required - contents.keys())
    if missing:
        raise PluginPackageError('PLUGIN_RUNTIME_INCOMPLETE', 'plugin runtime or entrypoints are missing',
                                 details={'missing': missing})
    for relative in ('plugin.json', '.claude-plugin/plugin.json', '.antigravity-plugin/plugin.json'):
        plugin = _json(contents[relative], relative)
        if plugin.get('name') != manifest['name'] or plugin.get('version') != manifest['version']:
            raise PluginPackageError('PLUGIN_VERSION_MISMATCH', 'plugin identity differs from toolbox.manifest.json',
                                     details={'path': relative})


def _freeze(root: Path) -> dict:
    state, raw = _read(root, 'toolbox.manifest.json')
    if raw is None:
        raise PluginPackageError('PLUGIN_RUNTIME_INCOMPLETE', 'toolbox.manifest.json is missing')
    manifest = _json(raw, 'toolbox.manifest.json')
    skills = manifest.get('skills')
    if (manifest.get('name') != 'govel' or not isinstance(manifest.get('version'), str)
            or not isinstance(skills, list) or not skills
            or not all(isinstance(name, str) and name.startswith('govel-') and '/' not in name
                       and '\\' not in name and name not in {'.', '..'} for name in skills)):
        raise PluginPackageError('PLUGIN_MANIFEST_INVALID', 'source does not declare a Govel runtime')
    candidates = _candidates(root)
    selected = [name for name in candidates if _runtime_path(name, set(skills))]
    records, contents = {}, {}
    for relative in selected:
        observed, data = (state, raw) if relative == 'toolbox.manifest.json' else _read(root, relative)
        records[relative] = observed
        if data is None:  # A tracked deletion is part of the current worktree.
            continue
        if issue := local_content_issue(relative, data):
            raise PluginPackageError('PLUGIN_PRIVATE_CONTENT', 'public local placeholder must be empty',
                                     details={'path': relative, 'issue': issue})
        contents[relative] = data
    release = check_paths(list(contents), public=True)
    if not release['ok']:
        raise PluginPackageError('PLUGIN_PUBLIC_CONTENT_INVALID', 'public release checks failed',
                                 details={'problems': release['problems']})
    _validate_metadata(contents, manifest)
    return {'root': root, 'manifest': manifest, 'skills': set(skills), 'selected': selected,
            'records': records, 'contents': contents, 'excludedCount': len(candidates) - len(selected)}


def _verify_source(snapshot: dict) -> None:
    root = snapshot['root']
    selected = [name for name in _candidates(root) if _runtime_path(name, snapshot['skills'])]
    if selected != snapshot['selected']:
        raise PluginPackageError('PLUGIN_SOURCE_CHANGED', 'runtime file membership changed during packaging')
    for relative, expected in snapshot['records'].items():
        if _read(root, relative)[0] != expected:
            raise PluginPackageError('PLUGIN_SOURCE_CHANGED', 'runtime input changed during packaging',
                                     details={'path': relative})


def plan(root: Path = ROOT) -> dict:
    snapshot = _freeze(Path(root).expanduser().resolve())
    _verify_source(snapshot)
    return {'ok': True, 'code': 'PLUGIN_PACKAGE_PLAN', 'version': snapshot['manifest']['version'],
            'fileCount': len(snapshot['contents']), 'sourceBytes': sum(map(len, snapshot['contents'].values())),
            'excludedCount': snapshot['excludedCount'], 'files': sorted(snapshot['contents'])}


def _write_zip(stream, snapshot: dict) -> None:
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, payload in sorted(snapshot['contents'].items()):
            info = zipfile.ZipInfo(relative)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | snapshot['records'][relative]['mode']) << 16
            archive.writestr(info, payload)


def _verify_zip(path: Path, snapshot: dict) -> None:
    release = check_zip(path, public=True)
    if not release['ok']:
        raise PluginPackageError('PLUGIN_PUBLIC_CONTENT_INVALID', 'built archive failed public release checks',
                                 details={'problems': release['problems']})
    with zipfile.ZipFile(path) as archive:
        if sorted(archive.namelist()) != sorted(snapshot['contents']):
            raise PluginPackageError('PLUGIN_ARCHIVE_MISMATCH', 'archive inventory differs from frozen input')
        for info in archive.infolist():
            expected = snapshot['records'][info.filename]
            if (not stat.S_ISREG(info.external_attr >> 16)
                    or stat.S_IMODE(info.external_attr >> 16) != expected['mode']
                    or hashlib.sha256(archive.read(info)).hexdigest() != expected['sha256']):
                raise PluginPackageError('PLUGIN_ARCHIVE_MISMATCH', 'archive file differs from frozen input',
                                         details={'path': info.filename})


def build_plugin(root: Path, output: Path) -> dict:
    root = Path(root).expanduser().resolve()
    requested = Path(output).expanduser().absolute()
    output = requested.parent.resolve() / requested.name
    if output == root or output.is_relative_to(root):
        raise PluginPackageError('PLUGIN_OUTPUT_INVALID', 'plugin archives must be written outside the source worktree')
    if output.suffix.lower() != '.zip':
        raise PluginPackageError('PLUGIN_OUTPUT_INVALID', 'plugin archive output must end in .zip')
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'plugin archive output already exists: {output}')
    snapshot = _freeze(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix='.govel-plugin-', suffix='.zip', dir=output.parent)
    temporary = Path(temporary_name)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, 'w+b') as stream:
            _write_zip(stream, snapshot)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            info = os.fstat(stream.fileno())
            expected = {'exists': True, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest(),
                        'mode': stat.S_IMODE(info.st_mode), 'dev': info.st_dev, 'ino': info.st_ino}
            size = info.st_size
        _verify_zip(temporary, snapshot)
        _verify_source(snapshot)
        if observation(temporary)[0] != expected:
            raise PluginPackageError('PLUGIN_ARCHIVE_MISMATCH', 'temporary archive changed after writing')
        exclusive_move(temporary, output)
        if observation(output)[0] != expected:
            raise PluginPackageError('PLUGIN_ARCHIVE_MISMATCH', 'published archive changed before confirmation')
        return {'ok': True, 'code': 'PLUGIN_PACKAGE_BUILT', 'version': snapshot['manifest']['version'],
                'output': str(output), 'fileCount': len(snapshot['contents']), 'bytes': size,
                'sha256': expected['sha256'], 'excludedCount': snapshot['excludedCount']}
    finally:
        try:
            current = temporary.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='完整 Govel Git 工作树；默认当前脚本所在仓库')
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--plan', action='store_true', help='只检查并列出公共插件文件，不写入')
    operation.add_argument('--output', type=Path, help='仓库外的新 ZIP 路径；不覆盖已有文件')
    args = parser.parse_args(argv)
    try:
        report = plan(args.root) if args.plan else build_plugin(args.root, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        report = {'ok': False, 'code': getattr(exc, 'code', 'PLUGIN_PACKAGE_FAILED'), 'error': str(exc),
                  'details': getattr(exc, 'details', {})}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
