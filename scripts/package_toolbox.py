#!/usr/bin/env python3
"""Build a local personal toolbox package with an auditable manifest.

This is a *local* packager, not a publisher. It exists so that "what is in the
archive" is a fact that can be checked rather than a claim:

    python3 scripts/package_toolbox.py --plan                  # dry run, writes nothing
    python3 scripts/package_toolbox.py --output dist           # directory package + MANIFEST.json
    python3 scripts/package_toolbox.py --output dist/x.zip     # zip package + MANIFEST.json
    python3 scripts/package_toolbox.py --check --manifest dist/MANIFEST.json [--zip dist/x.zip]

Three levels are reported separately and never merged into one green light:

    build outcome               PACKAGE_BUILT / PACKAGE_MISMATCH — did the archive
                                get written and verify against its manifest
    localPersonalReleaseReady   private originals, caches and credentials excluded,
                                every symlink self-contained inside the archive
                                (blockers: localReleaseBlockers)
    publicReleaseReady          additionally requires a chosen license and cleared
                                source rights — an author decision the tool will not
                                make (blockers: publicReleaseBlockers)

`publicReleaseReady` stays blocked as ``PUBLIC_RELEASE_LICENSE_UNDECIDED`` while the
repository has no root LICENSE. Picking MIT/Apache/GPL on the author's behalf is
not something a packager gets to do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import stat
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

try:
    from scripts.core.project import TOOLBOX_ROOT, toolbox_manifest
except ImportError:  # running from inside scripts/
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.core.project import TOOLBOX_ROOT, toolbox_manifest

MANIFEST_NAME = 'MANIFEST.json'

# 明确排除的类别。每一项都要能说清「为什么它不该进发行包」；说不清的不要凭猜测删。
# path 类是前缀匹配；noise 类是「任一路径分量命中即排除」——它们可以出现在任何深度。
EXCLUSIONS: dict[str, tuple[str, str, str]] = {
    'vcs_metadata': ('.git', 'Git 元数据不是发行内容', 'prefix'),
    'private_reference_alias': ('小说参考', '真实参考小说原文的旧路径别名，只在本机', 'prefix'),
    'private_source': ('knowledge/sources/private/', '私有原文与迁移回执，Git 已忽略', 'prefix'),
    'derived_private_state': ('knowledge/.cache/', '可重建派生索引', 'prefix'),
    'derived_private_state_2': ('knowledge/.history/', '可重建资料版本历史', 'prefix'),
    'local_tool_state': ('.claude/settings.local.json', '本机权限文件', 'prefix'),
    'local_tool_worktrees': ('.claude/worktrees/', '本机代理工作区，非仓库内容', 'prefix'),
    'build_artifact': ('dist/', '打包输出自身', 'prefix'),
    'build_artifact_2': ('build/', '构建中间产物', 'prefix'),
    'egg_info': ('.egg-info/', '打包元数据', 'prefix'),
    'bytecode': ('__pycache__', 'Python 字节码缓存', 'component'),
    'pytest_cache': ('.pytest_cache', '测试缓存', 'component'),
    'finder_noise': ('.DS_Store', 'Finder 元数据', 'component'),
    'agent_trash': ('.trash-', '安装器安全备份目录', 'component-prefix'),
    'migration_review_copy': ('migration-review.json', '临时迁移审查副本，含未决段落与原文定位', 'component'),
}

CREDENTIAL_NAMES = {'.env', '.env.local', 'id_rsa', 'id_ed25519', 'credentials.json',
                    'secrets.json', 'cookies.txt', '.netrc', '.pypirc'}
CREDENTIAL_SUFFIXES = ('.pem', '.p12', '.pfx', '.key')


def _unsafe(name: str) -> str | None:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or not name:
        return 'unsafe_path'
    parts = set(path.parts)
    for category, (needle, _reason, kind) in EXCLUSIONS.items():
        if kind == 'prefix':
            if name == needle or name.startswith(needle if needle.endswith('/') else needle + '/'):
                return category
        elif needle in parts or (kind == 'component-prefix'
                                 and any(part.startswith(needle) for part in parts)):
            return category
    if name.startswith('.git/') or name == '.git':
        return 'vcs_metadata'
    lower = path.name.casefold()
    if lower in CREDENTIAL_NAMES or lower.endswith(CREDENTIAL_SUFFIXES):
        return 'credential_candidate'
    return None


def _machine_home_prefixes() -> list[str]:
    home = Path.home()
    return [str(home) + '/', str(home)]


def _local_absolute_paths(text: str) -> list[str]:
    """Report this machine's absolute paths referenced inside a text file.

    Only the *current* machine's home prefix counts: a path like `/usr/bin/python3`
    is a normal system path, while `/Users/<name>/...` pins the archive to one
    computer and leaks the account name.
    """
    found = []
    for prefix in _machine_home_prefixes():
        start = 0
        while True:
            index = text.find(prefix, start)
            if index < 0:
                break
            end = index
            while end < len(text) and text[end] not in ' \t\n\r`"\'<>,;)]':
                end += 1
            found.append(text[index:end])
            start = index + len(prefix)
    return sorted(set(found))


def _candidates() -> list[str]:
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            cwd=TOOLBOX_ROOT, capture_output=True, check=True)
    return [part.decode('utf-8') for part in result.stdout.split(b'\0') if part]


def _packaged_under(names: set[str], combined: str) -> bool:
    """链接目标可以是包内文件，也可以是包内有打包内容的目录。

    目录目标（如 dashboard/garden -> 月海花庭/月海花庭）解压后自洽：只要该目录
    下至少有一个打包内容，链接即落回包内。镜像于 check_release_contents.py。
    """
    return combined in names or any(name.startswith(combined + '/') for name in names)


def _symlink_archive_state(name: str, target: str, names: set[str]) -> tuple[str, str | None]:
    """Judge a symlink the way the archive will hold it: by its *raw* target.

    Where the link happens to resolve on this machine is irrelevant after
    extraction. A raw target that is absolute, leaves the package, lands on
    excluded content, or does not land on packaged content cannot be reproduced
    self-contained. `collect()` and `check()` share this rule so a built package
    can never fail its own verification.
    """
    combined = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if (target.startswith('/') or combined.startswith('../') or combined == '..'
            or _unsafe(combined) or not _packaged_under(names, combined)):
        return combined, 'unsafe_archive_symlink'
    return combined, None


def _unicode_collisions(names: set[str]) -> list[dict]:
    normalized: dict[str, str] = {}
    problems: list[dict] = []
    for name in sorted(names):
        key = unicodedata.normalize('NFC', name).casefold()
        if key in normalized and normalized[key] != name:
            problems.append({'path': name, 'code': 'unicode_path_collision', 'other': normalized[key]})
        normalized[key] = name
    return problems


def collect() -> dict:
    """Split the working-tree candidates into packaged files, links and exclusions.

    Symlinks are recorded with their *raw* target rather than a content hash: the
    package must reproduce the link as authored, so it stays self-contained. A
    link is packaged only if the archive rule accepts it (``_symlink_archive_state``
    — the same rule ``--check`` applies); anything else is excluded with an
    auditable category instead of making the built package fail its own check.
    """
    packaged: dict[str, str] = {}
    links: dict[str, str] = {}
    excluded: dict[str, dict] = {}
    seen: set[str] = set()
    link_targets: dict[str, str] = {}
    for name in sorted(_candidates()):
        if name in seen:
            excluded[name] = {'category': 'duplicate_archive_path', 'reason': '重复路径'}
            continue
        seen.add(name)
        category = _unsafe(name)
        if category:
            reason = next((value[1] for key, value in EXCLUSIONS.items() if key == category),
                          '不得进入发行候选')
            excluded[name] = {'category': category, 'reason': reason}
            continue
        path = TOOLBOX_ROOT / name
        if path.is_symlink():
            link_targets[name] = os.readlink(path)
            continue
        if not path.is_file():
            continue
        packaged[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # 链条合流：指向另一个被拒链接的链接同样无法在包内自洽，收敛到不动点。
    accepted = set(link_targets)
    changed = True
    while changed:
        changed = False
        names = set(packaged) | accepted
        for name in sorted(accepted):
            if _symlink_archive_state(name, link_targets[name], names)[1] is not None:
                accepted.discard(name)
                changed = True
    for name in sorted(link_targets):
        if name in accepted:
            links[name] = link_targets[name]
        else:
            excluded[name] = {'category': 'external_or_broken_symlink',
                              'reason': '软链越出仓库或指向未打包/受排除内容，打包后无法自洽复现'}
    return {'packaged': packaged, 'links': links, 'excluded': excluded}


def _local_path_references(packaged: dict[str, str]) -> dict[str, list[str]]:
    """Scan text candidates for this machine's absolute paths.

    A documented reference to where the real book lives is legitimate prose, not
    a machine dependency, so it is reported per file and never silently dropped:
    local release tolerates it, public release does not.
    """
    findings: dict[str, list[str]] = {}
    for name in packaged:
        path = TOOLBOX_ROOT / name
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        hits = _local_absolute_paths(text)
        if hits:
            findings[name] = hits
    return findings


def build_manifest(*, mode: str, local_paths: dict[str, list[str]]) -> dict:
    collected = collect()
    packaged = collected['packaged']
    links = collected['links']
    manifest = toolbox_manifest()
    root_license = (TOOLBOX_ROOT / 'LICENSE').is_file()
    license_state = 'ROOT_LICENSE_PRESENT' if root_license else 'PUBLIC_RELEASE_LICENSE_UNDECIDED'
    legacy_sources = sorted(name for name in packaged if name.startswith('knowledge/sources/legacy/'))
    public_blockers = []
    if not root_license:
        public_blockers.append({'code': 'PUBLIC_RELEASE_LICENSE_UNDECIDED',
                                'detail': '根目录尚未选择公开许可证；这是作者决定，打包器不代为选择'})
    if legacy_sources:
        public_blockers.append({'code': 'SOURCE_RIGHTS_NOT_CLEARED',
                                'detail': f'{len(legacy_sources)} 个旧来源原件权利未逐份复核'})
    if local_paths:
        public_blockers.append({'code': 'LOCAL_ABSOLUTE_PATH_REFERENCE',
                                'detail': f'{len(local_paths)} 个文件引用本机绝对路径'})
    entries = ([{'path': name, 'kind': 'file', 'sha256': packaged[name], 'bytes': _size(name)}
                for name in sorted(packaged)]
               + [{'path': name, 'kind': 'symlink', 'target': links[name]} for name in sorted(links)])
    entries.sort(key=lambda row: row['path'])
    # 本地就绪必须是断言，不是常量：凡是会让包自身 --check 失败的残余冲突
    # （如 NFC/casefold 路径碰撞）都进 localReleaseBlockers，build 不再先说 ready。
    local_blockers = _unicode_collisions(set(packaged) | set(links))
    return {
        'schemaVersion': 1,
        'toolboxVersion': str(manifest['version']),
        'generatedAt': _now(),
        'mode': mode,
        'projectShape': 'local_personal',
        'fileCount': len(entries),
        'linkCount': len(links),
        'files': entries,
        'excludedCategories': sorted({row['category'] for row in collected['excluded'].values()}),
        'excluded': {name: row for name, row in sorted(collected['excluded'].items())},
        'localAbsolutePathReferences': {name: hits for name, hits in sorted(local_paths.items())},
        'readiness': {
            'localPersonalReleaseReady': not local_blockers,
            'localReleaseBlockers': local_blockers,
            'publicReleaseReady': not local_blockers and not public_blockers,
            'publicReleaseBlockers': public_blockers,
            'license': license_state,
            'rootLicensePresent': root_license,
            'legacySourceCount': len(legacy_sources),
        },
    }


def _now() -> str:
    try:
        from scripts.core.clock import utc_now
    except ImportError:
        from core.clock import utc_now
    return utc_now()


def _size(name: str) -> int:
    path = TOOLBOX_ROOT / name
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _write_manifest(manifest: dict, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return destination


def build_directory(output: Path, manifest: dict) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    written = []
    for row in manifest['files']:
        source = TOOLBOX_ROOT / row['path']
        target = output / row['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            target.write_bytes(source.read_bytes())
        written.append(row['path'])
    _write_manifest(manifest, output / MANIFEST_NAME)
    return {'ok': True, 'code': 'PACKAGE_BUILT', 'output': str(output),
            'fileCount': len(written), 'manifest': str(output / MANIFEST_NAME)}


def build_zip(output: Path, manifest: dict) -> dict:
    """Write the archive plus a sidecar manifest next to it.

    The sidecar is what `--check` reads: it must exist independently of the zip so
    the author can verify an archive they copied elsewhere.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    sidecar = output.parent / (output.name + '.MANIFEST.json')
    _write_manifest(manifest, sidecar)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for row in manifest['files']:
            source = TOOLBOX_ROOT / row['path']
            # from_file follows links and appends '/' for directory targets.
            # Preserve the link's own manifest name instead of its target's type.
            info = (zipfile.ZipInfo(row['path']) if source.is_symlink()
                    else zipfile.ZipInfo.from_file(source, row['path']))
            info.compress_type = zipfile.ZIP_DEFLATED
            if source.is_symlink():
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, os.readlink(source))
            else:
                with source.open('rb') as handle:
                    archive.writestr(info, handle.read())
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return {'ok': True, 'code': 'PACKAGE_BUILT', 'output': str(output),
            'fileCount': len(manifest['files']), 'manifest': str(sidecar)}


def check(manifest_path: Path, archive: Path | None) -> dict:
    """Verify an existing package against its manifest.

    A missing file, an extra file, or a changed hash all fail. The check refuses
    to report success while an excluded category is present — leaking a private
    original is exactly the failure this exists to catch.
    """
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    problems: list[dict] = []
    if archive is not None:
        with zipfile.ZipFile(archive) as handle:
            present = {name for name in handle.namelist() if name != MANIFEST_NAME}
            bodies = {name: handle.read(name) for name in present if not stat.S_ISLNK(
                handle.getinfo(name).external_attr >> 16)}
            links = {name: handle.read(name).decode('utf-8', errors='replace')
                     for name in handle.namelist()
                     if stat.S_ISLNK(handle.getinfo(name).external_attr >> 16)}
    else:
        root = manifest_path.parent
        present, bodies, links = set(), {}, {}
        for path in sorted(root.rglob('*')):
            if path.name == MANIFEST_NAME and path.parent == root:
                continue
            if not (path.is_file() or path.is_symlink()):
                continue
            relative = str(path.relative_to(root))
            present.add(relative)
            if path.is_symlink():
                # 用 readlink 的原样目标，不用 resolve() 的绝对路径：判定「是否越出包外」
                # 要看链接本身指向哪里，而不是它在本机被解析到哪里。
                links[relative] = os.readlink(path)
            else:
                bodies[relative] = path.read_bytes()
    expected = {row['path']: row for row in manifest['files']}
    expected_links = {name: row.get('target') for name, row in expected.items()
                      if row.get('kind') == 'symlink'}
    for name in sorted(expected.keys() - present):
        problems.append({'path': name, 'code': 'manifest_file_missing'})
    for name in sorted(present - expected.keys()):
        problems.append({'path': name, 'code': 'package_file_not_in_manifest'})
    for name in sorted(expected.keys() & present):
        row = expected[name]
        if name in links or row.get("kind") == "symlink":
            target = links.get(name)
            if target is None:
                problems.append({'path': name, 'code': 'symlink_became_regular_file'})
                continue
            if target != expected_links.get(name):
                problems.append({'path': name, 'code': 'symlink_target_mismatch',
                                 'expected': expected_links.get(name), 'actual': target})
                continue
            combined, problem = _symlink_archive_state(name, target, set(expected))
            if problem is not None:
                problems.append({'path': name, 'code': problem,
                                 'target': target, 'resolved': combined})
            continue
        digest = hashlib.sha256(bodies[name]).hexdigest()
        if digest != row['sha256']:
            problems.append({'path': name, 'code': 'hash_mismatch', 'expected': row['sha256'],
                             'actual': digest})
    for name in present:
        category = _unsafe(name)
        if category:
            problems.append({'path': name, 'code': 'excluded_category_present', 'category': category})
    problems.extend(_unicode_collisions(set(present) | set(expected)))
    return {'ok': not problems, 'code': 'PACKAGE_VERIFIED' if not problems else 'PACKAGE_MISMATCH',
            'manifest': str(manifest_path), 'archive': str(archive) if archive else None,
            'expectedFileCount': len(expected), 'presentFileCount': len(present),
            'problems': problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, help='打包输出：目录或以 .zip 结尾的压缩包')
    parser.add_argument('--check', action='store_true', help='只校验已有包，不打包')
    parser.add_argument('--manifest', type=Path, help='--check 时指定 MANIFEST.json')
    parser.add_argument('--zip', type=Path, help='--check 时指定待校验的 zip')
    parser.add_argument('--plan', action='store_true', help='干跑：只报告将打包/排除什么')
    parser.add_argument('--public', action='store_true', help='按公开发布口径评估（仍不替你选许可证）')
    args = parser.parse_args(argv)

    if args.check:
        if not args.manifest:
            parser.error('--check 需要 --manifest')
        report = check(args.manifest, args.zip)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['ok'] else 1

    local_paths = _local_path_references(collect()['packaged'])
    manifest = build_manifest(mode='public-audit' if args.public else 'local-personal',
                             local_paths=local_paths)
    if args.plan:
        plan = {'ok': True, 'code': 'PACKAGE_PLAN', 'fileCount': manifest['fileCount'],
                'excludedCategories': manifest['excludedCategories'],
                'excludedCount': len(manifest['excluded']),
                'localAbsolutePathReferences': sorted(manifest['localAbsolutePathReferences']),
                'readiness': manifest['readiness']}
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not args.output:
        parser.error('打包需要 --output（目录或 .zip）；只要报告请用 --plan')

    if args.output.suffix == '.zip':
        report = build_zip(args.output, manifest)
        report['manifest'] = report['manifest'] + '（包内另有一份）'
    else:
        report = build_directory(args.output, manifest)
    report['readiness'] = manifest['readiness']
    report['excludedCategories'] = manifest['excludedCategories']
    report['localAbsolutePathReferences'] = sorted(manifest['localAbsolutePathReferences'])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 本地私用包可以带着本机路径引用发出；公开发布口径下同样内容必须先处理这些引用。
    return 0 if manifest['readiness']['localPersonalReleaseReady'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
