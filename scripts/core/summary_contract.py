"""The summary envelope contract shared by preparation, submission and readers."""
from __future__ import annotations

import json
from pathlib import Path

from .project import current_schema_version


def validate_summary(payload) -> None:
    if not isinstance(payload, dict):
        raise ValueError('摘要 JSON 必须是对象')
    version = current_schema_version()
    if type(payload.get('schemaVersion')) is not int or payload['schemaVersion'] != version:
        raise ValueError(f'摘要 schemaVersion 不支持；当前格式版本为 {version}')
    for field in ('asOfChapterId', 'storySummary'):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f'摘要 {field} 必须是非空文字')
    for field, identity in (('plotLine', 'chapterId'), ('characterSummaries', 'characterId')):
        if field not in payload:
            continue
        rows = payload[field]
        if not isinstance(rows, list):
            raise ValueError(f'摘要 {field} 必须是数组')
        seen = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get(identity), str) or not row[identity].strip():
                raise ValueError(f'摘要 {field}[{index}].{identity} 必须是稳定 ID')
            if row[identity] in seen:
                raise ValueError(f'摘要 {field}[{index}].{identity} 重复')
            seen.add(row[identity])
            if not isinstance(row.get('summary'), str):
                raise ValueError(f'摘要 {field}[{index}].summary 必须是文字')


def load_summary(root: Path) -> dict | None:
    target = root / '.govel/aux/story-summary.json'
    if not target.is_file() or target.is_symlink():
        return None
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
        validate_summary(payload)
        return payload
    except (OSError, ValueError):
        return None
