"""On-demand human snapshots; no generated-view maintenance or model rewriting."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .commands import resolve_export_target
from .jsonio import write_bytes
from .locks import project_lock
from .project import query_policy
from .query import _size
from .source_selection import select_project_content
from .structure_store import StructureError, canonical_json

SNAPSHOT_PREFIX = '<!-- gnovel-SNAPSHOT '


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def render_selection(result: dict, *, require_reviewed: bool = False) -> str:
    blocked = [r['id'] for r in result['records']
               if (r['kind'] == 'fragment' and r.get('reviewStatus') not in {'compatible', 'resolved'})
               or (r['kind'] == 'value' and r['content'].get('state', 'known') not in {'known', 'not_applicable'})]
    if require_reviewed and blocked:
        raise StructureError('SOURCE_REVIEW_REQUIRED', 'selection includes unreviewed, unresolved or unknown content', {'recordIds': blocked})
    snapshot, choice = result['snapshot'], result['selection']
    lines = [f"# {result['bookName']} · 设定与规划", '', f"> 来源版本：{snapshot['revision']}；源更新时间：{result['sourceUpdatedAt']}", '']
    excluded = [result['fieldLabels'][key] for key in choice.get('excludeFields', [])]
    if excluded:
        lines.extend(['排除字段：' + '、'.join(excluded), ''])
    if 'asOf' in choice:
        lines.extend(['故事时点：' + (result['timeLabels'][choice['asOf']]
                                    if choice['asOf'] is not None else '尚未确定'), ''])
    for warning in result['warnings']:
        lines.extend(['> ' + warning, ''])
    if result['coverage']['unknownTimeCount']:
        lines.extend([f"> 时间无法确定的内容：{result['coverage']['unknownTimeCount']} 项；处理方式："
                      + ('保留并标明。' if choice['unknownTime'] == 'include' else '排除。'), ''])
    labels = {row['id']: row['label'] for row in result['owners']}
    owner = None
    for record in result['records']:
        if owner != record['ownerId']:
            owner = record['ownerId']; lines.extend(['## ' + labels[owner], ''])
        content = record['content']
        period = content.get('validDuring') or {}
        start, end = period.get('fromId'), period.get('toId')
        terms = []
        if start: terms.append(result['timeLabels'][start] + '起')
        if end: terms.append(result['timeLabels'][end] + '之前')
        if terms: lines.extend(['适用阶段：' + '，'.join(terms), ''])
        if record['timeMatch'] == 'unknown': lines.extend(['> 与查询时点的关系尚未确定。', ''])
        if record['kind'] == 'fragment':
            if record.get('reviewStatus') in {'pending', 'stale', 'conflict', 'deferred', 'needs_information'}:
                lines.extend(['> 此段存在待处理的设定复核，请结合当前规范属性阅读。', ''])
            role = {'belief': '人物理解', 'in_world_text': '世界内文献', 'plan': '剧情计划'}.get(content['contentRole'])
            if role: lines.extend(['内容身份：' + role, ''])
            lines.extend([content['text'], ''])
            if content.get('chapterContract') is not None:
                lines.extend(['章节合同（随源计划批准）：', '', '```json',
                              json.dumps(content['chapterContract'], ensure_ascii=False, indent=2), '```', ''])
        else:
            state = content.get('state', 'known')
            value = content.get('value')
            if state != 'known':
                text = {'unknown': '未确定', 'unfilled': '尚未填写', 'not_applicable': '不适用'}[state]
            elif isinstance(value, dict):
                text = f"{value['number']} {value['unit']}"
            elif type(value) is bool:
                text = '是' if value else '否'
            elif record.get('valueType') == 'reference':
                text = result['referenceLabels'][value]
            else:
                text = str(value)
            lines.extend([f"- {record['label']}：{text}", ''])
    if not result['records']:
        lines.extend(['本次筛选没有选中内容。', ''])
    body = '\n'.join(lines)
    marker = {'sourceHash': snapshot['sourceHash'], 'revision': snapshot['revision'],
              'selectionHash': _hash(canonical_json(choice)), 'bodyHash': _hash(body)}
    return SNAPSHOT_PREFIX + json.dumps(marker, ensure_ascii=False, separators=(',', ':')) + ' -->\n' + body


def render_source(root: Path, *, selection: dict | None = None, presetId: str | None = None,
                  requireReviewed: bool | None = None,
                  maxBytes: int | None = None) -> dict:
    from .source_presets import resolve_preset
    selection, preset = resolve_preset(root, selection=selection, presetId=presetId)
    if requireReviewed is None:
        requireReviewed = bool(preset and preset['layout']['requireReviewed'])
    selected = select_project_content(root, selection)
    markdown = render_selection(selected, require_reviewed=requireReviewed)
    result = {'ok': True, 'code': 'SOURCE_RENDERED', 'snapshot': selected['snapshot'],
              'selection': selected['selection'], 'recordIds': [r['id'] for r in selected['records']],
              'coverage': selected['coverage'], 'markdown': markdown}
    if preset is not None:
        result['preset'] = preset
    maximum = query_policy()['maxBytes']['detail']
    budget = maximum if maxBytes is None else maxBytes
    if type(budget) is not int or not 1 <= budget <= maximum:
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid render byte budget')
    if _size(result) > budget:
        raise StructureError('SOURCE_EXPORT_REQUIRED', 'complete reading text exceeds response budget; request a file export')
    return result


def export_source(root: Path, *, output: str, selection: dict | None = None, presetId: str | None = None,
                  requireReviewed: bool | None = None, overwrite: bool = False, expectedFileHash: str | None = None) -> dict:
    if not isinstance(output, str) or not output.strip():
        raise StructureError('SOURCE_EXPORT_TARGET_REQUIRED', 'an explicit reading-file destination is required')
    with project_lock(root):
        from .source_presets import resolve_preset
        selection, preset = resolve_preset(root, selection=selection, presetId=presetId)
        if requireReviewed is None:
            requireReviewed = bool(preset and preset['layout']['requireReviewed'])
        selected = select_project_content(root, selection)
        text = render_selection(selected, require_reviewed=requireReviewed)
        target = resolve_export_target(root, Path(output), book_name=selected['bookName'], fmt='md')
        payload = text.encode()
        if target.exists():
            old = target.read_bytes()
            current_hash = hashlib.sha256(old).hexdigest()
            if expectedFileHash is not None and expectedFileHash != current_hash:
                raise StructureError('SOURCE_EXPORT_CONFLICT', 'target changed after inspection')
            if old != payload:
                if not overwrite or expectedFileHash is None:
                    raise StructureError('SOURCE_EXPORT_EXISTS', 'choose a new snapshot path or explicitly overwrite the inspected file', {'sha256': current_hash})
        write_bytes(target, payload)
        return {'ok': True, 'code': 'SOURCE_EXPORTED', 'output': str(target), 'sha256': hashlib.sha256(payload).hexdigest(),
                'bytes': len(payload), 'snapshot': selected['snapshot'], 'selection': selected['selection'],
                'recordCount': len(selected['records']), 'coverage': selected['coverage'], 'maintained': False,
                **({'preset': preset} if preset is not None else {})}
