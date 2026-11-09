#!/usr/bin/env python3
"""Audit a fresh source tree with an external, non-distributed review word list."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import stat
import sys
import unicodedata
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.core.local_layout import private_local_path, local_content_issue
OMIT = {'.git', '__pycache__', '.pytest_cache', 'node_modules', 'dist', 'build'}


def review_terms(words):
    if not isinstance(words, list) or not words or any(not isinstance(w, str) or not w for w in words):
        raise ValueError('review list must be a nonempty JSON array of strings')
    patterns = []
    for word in words:
        normalized = _word_text(word)
        if word.isascii() and any(char.isalnum() for char in word):
            # Identifier boundaries matter: the final letter of one camelCase
            # word cannot combine with the first letters of the next word.
            patterns.append(re.compile(r'(?<![a-z0-9])' + re.escape(normalized) + r'(?![a-z0-9])'))
        else:
            patterns.append(re.compile(re.escape(normalized)))
    return patterns


def _word_text(value):
    value = unicodedata.normalize('NFKC', value)
    value = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', value)
    value = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', value)
    return value.casefold()


def _entry(relative, data, patterns):
    problems = []
    if private_local_path(relative):
        problems.append({'path': relative, 'code': 'USER_DATA_IN_SOURCE'})
    if issue := local_content_issue(relative, data):
        problems.append({'path': relative, 'code': issue})
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        return problems + [{'path': relative, 'code': 'NON_TEXT_FILE'}]
    combined = _word_text(relative + '\n' + text)
    ids = [index for index, pattern in enumerate(patterns) if pattern.search(combined)]
    if ids:
        problems.append({'path': relative, 'code': 'REVIEW_TERM', 'termIndexes': ids})
    if relative.startswith(('knowledge/assets/', 'knowledge/sources/', 'knowledge/.cache/', 'knowledge/.history/')):
        problems.append({'path': relative, 'code': 'USER_DATA_IN_SOURCE'})
    if Path(relative).name in {'provider.json', 'provider-config.json', '.env', 'credentials.json', 'cookies.txt'}:
        problems.append({'path': relative, 'code': 'LOCAL_CONFIGURATION'})
    return problems


def audit(root, words):
    patterns = review_terms(words)
    problems, files = [], []
    # Ignored data may live in the checkout. Forced additions still appear in
    # this candidate set and are rejected just like archive/history entries.
    if (root / '.git').exists():
        names = _git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
        paths = [root / name.decode('utf-8') for name in sorted(set(names.split(b'\0'))) if name]
    else:
        paths = sorted(root.rglob('*'))
    for path in paths:
        relative = path.relative_to(root)
        if any(part in OMIT for part in relative.parts):
            continue
        if path.is_symlink():
            problems.append({'path': str(relative), 'code': 'SYMLINK_NOT_ALLOWED'})
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        files.append({'path': str(relative), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
        problems.extend(_entry(str(relative), data, patterns))
    return {'ok': not problems, 'code': 'RELEASE_CONTENT_AUDIT', 'fileCount': len(files),
            'reviewTerms': len(words), 'files': files, 'problems': problems,
            'scope': 'Git candidates or unpacked source tree; lexical review does not replace contextual review'}


def audit_zip(path, words):
    patterns = review_terms(words)
    problems, files, seen = [], [], set()
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            relative = entry.filename
            if relative in seen:
                problems.append({'path': relative, 'code': 'DUPLICATE_ARCHIVE_ENTRY'})
            seen.add(relative)
            if (relative.startswith('/') or '..' in Path(relative).parts or '\\' in relative
                    or any(part in OMIT for part in Path(relative).parts)):
                problems.append({'path': relative, 'code': 'UNEXPECTED_ARCHIVE_PATH'})
            if stat.S_ISLNK(entry.external_attr >> 16):
                problems.append({'path': relative, 'code': 'SYMLINK_NOT_ALLOWED'})
            if entry.is_dir():
                continue
            data = archive.read(entry)
            files.append({'path': relative, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
            problems.extend(_entry(relative, data, patterns))
    return {'ok': not problems, 'code': 'RELEASE_ARCHIVE_AUDIT', 'fileCount': len(files),
            'reviewTerms': len(words), 'files': files, 'problems': problems,
            'scope': 'all ZIP entries, including names and embedded manifest'}


def _git(root, *args, **kwargs):
    return subprocess.run(['git', *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True, **kwargs).stdout


def _objects(root, identities):
    raw = _git(root, 'cat-file', '--batch', input=('\n'.join(identities) + '\n').encode())
    position, result = 0, {}
    for identity in identities:
        end = raw.index(b'\n', position)
        oid, kind, length = raw[position:end].decode('ascii').split()
        if oid != identity or kind not in {'commit', 'blob'}:
            raise ValueError('Git object is missing or has an unexpected type')
        size = int(length)
        result[identity] = raw[end + 1:end + 1 + size]
        if len(result[identity]) != size or raw[end + 1 + size:end + 2 + size] != b'\n':
            raise ValueError('Git object output is incomplete')
        position = end + 2 + size
    return result


def audit_history(root, words):
    patterns = review_terms(words)
    commits = _git(root, 'rev-list', '--all').decode().splitlines()
    if not commits:
        raise ValueError('Git history is empty')
    entries, problems = set(), []
    for commit in commits:
        for row in _git(root, 'ls-tree', '-r', '-z', '--full-tree', commit).split(b'\0'):
            if not row:
                continue
            header, path = row.split(b'\t', 1)
            mode, kind, identity = header.decode().split()
            relative = path.decode('utf-8')
            if kind != 'blob' or mode not in {'100644', '100755'}:
                problems.append({'path': relative, 'code': 'NON_REGULAR_GIT_ENTRY', 'commit': commit})
                continue
            entries.add((relative, identity))
    identities = sorted(set(commits) | {identity for _, identity in entries})
    objects = _objects(root, identities)
    for commit in commits:
        problems.extend({**issue, 'commit': commit} for issue in _entry('commit-message', objects[commit], patterns))
    for relative, identity in sorted(entries):
        problems.extend({**issue, 'object': identity} for issue in _entry(relative, objects[identity], patterns))
    return {'ok': not problems, 'code': 'RELEASE_HISTORY_AUDIT', 'commitCount': len(commits),
            'fileVersions': len(entries), 'reviewTerms': len(words), 'problems': problems,
            'scope': 'all reachable commits and every regular file version across all refs'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--terms', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--zip', type=Path, help='audit every entry in an existing release archive')
    source.add_argument('--history', action='store_true', help='audit all reachable Git history')
    args = parser.parse_args(argv)
    if args.terms.resolve().is_relative_to(ROOT):
        parser.error('review words must be outside the source tree')
    if args.output and args.output.resolve().is_relative_to(ROOT):
        parser.error('audit reports must be outside the source tree')
    try:
        words = json.loads(args.terms.read_text())
        report = (audit_zip(args.zip, words) if args.zip else audit_history(ROOT, words)
                  if args.history else audit(ROOT, words))
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        report = {'ok': False, 'code': 'RELEASE_AUDIT_FAILED', 'message': str(exc)}
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'files'}, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
