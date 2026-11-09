#!/usr/bin/env python3
"""Read-only check of files that would enter a local or public toolbox archive."""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import subprocess
import unicodedata
import zipfile
import stat
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.local_layout import private_local_path, local_content_issue

def _unsafe(name: str) -> str | None:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or not name or name.startswith('/'):
        return 'unsafe_path'
    if private_local_path(name):
        return 'local_user_data'
    if path.name in {'provider-config.json', 'provider.json'}:
        return 'provider_configuration'
    if name == '小说参考' or name.startswith('小说参考/'):
        return 'private_reference_alias'
    if name.startswith('knowledge/sources/private/') or name == 'knowledge/sources/private':
        return 'private_source'
    if name.startswith('knowledge/.cache/') or name.startswith('knowledge/.history/'):
        return 'derived_private_state'
    lower = path.name.casefold()
    if (lower == '.env' or lower.endswith(('.pem', '.p12', '.pfx', '.key'))
            or lower in {'id_rsa', 'id_ed25519', 'credentials.json', 'secrets.json'}):
        return 'credential_candidate'
    return None


def check_paths(names: list[str], *, public: bool = False) -> dict:
    problems, normalized, seen = [], {}, set()
    for name in names:
        if name in seen:
            problems.append({'path': name, 'code': 'duplicate_archive_path'})
        seen.add(name)
        cause = _unsafe(name)
        if cause:
            problems.append({'path': name, 'code': cause})
        key = unicodedata.normalize('NFC', name).casefold()
        if key in normalized and normalized[key] != name:
            problems.append({'path': name, 'code': 'unicode_path_collision', 'other': normalized[key]})
        normalized[key] = name
    if public:
        if 'LICENSE' not in names:
            problems.append({'path': 'LICENSE', 'code': 'root_license_missing'})
        if any(name.startswith('knowledge/sources/legacy/') for name in names):
            problems.append({'path': 'knowledge/sources/legacy/', 'code': 'source_rights_not_cleared'})
    return {'ok': not problems, 'code': 'RELEASE_CONTENTS', 'fileCount': len(names),
            'mode': 'public' if public else 'local-private', 'problems': problems}


def _packaged_under(names: set[str], combined: str) -> bool:
    """链接目标可以是包内文件，也可以是包内有打包内容的目录（镜像打包器判定）。"""
    return combined in names or any(name.startswith(combined + '/') for name in names)


def check_worktree(*, public: bool = False) -> dict:
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            cwd=TOOLBOX_ROOT, capture_output=True, check=True)
    names = [part.decode('utf-8') for part in result.stdout.split(b'\0') if part]
    report = check_paths(names, public=public)
    candidate_names = set(names)
    for name in names:
        path = TOOLBOX_ROOT / name
        if path.is_file() and not path.is_symlink():
            if issue := local_content_issue(name, path.read_bytes() if name.endswith('.gitkeep') else b''):
                report['problems'].append({'path': name, 'code': issue})
        if path.is_symlink():
            # 与 check_zip / 打包器同一判定：看链接本身指向哪里（原样 target），
            # 不看它在本机被解析到哪里——否则同一棵树在打包入口与校验入口得到相反结论。
            target = os.readlink(path)
            combined = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if (target.startswith('/') or combined.startswith('../') or combined == '..'
                    or _unsafe(combined) or not _packaged_under(candidate_names, combined)):
                report['problems'].append({'path': name, 'code': 'unsafe_archive_symlink'})
    report['ok'] = not report['problems']
    return report


def check_zip(path: Path, *, public: bool = False) -> dict:
    with zipfile.ZipFile(path) as archive:
        report = check_paths(archive.namelist(), public=public)
        names = set(archive.namelist())
        for item in archive.infolist():
            if issue := local_content_issue(item.filename, archive.read(item) if not item.is_dir() else b''):
                report['problems'].append({'path': item.filename, 'code': issue})
            if stat.S_ISLNK(item.external_attr >> 16):
                # ZIP symlinks can resolve outside the archive or into ignored
                # private material when an extractor follows them.
                target = archive.read(item).decode('utf-8', errors='replace')
                combined = posixpath.normpath(posixpath.join(posixpath.dirname(item.filename), target))
                if (target.startswith('/') or combined.startswith('../') or combined == '..'
                        or _unsafe(combined) or not _packaged_under(names, combined)):
                    report['problems'].append({'path': item.filename, 'code': 'unsafe_archive_symlink'})
        report['ok'] = not report['problems']
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Check toolbox files before packaging')
    parser.add_argument('--zip', type=Path)
    parser.add_argument('--public', action='store_true')
    args = parser.parse_args()
    result = check_zip(args.zip, public=args.public) if args.zip else check_worktree(public=args.public)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
