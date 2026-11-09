"""Triage view over outstanding source reviews: cluster by origin, layer by relation.

pending/stale/conflict/needs_information/not_checked 是需要人工处理的待办。真实项目里它们常以
百计，逐条扫读的成本在「从哪来」而不是「有多少」：本视图把待处理项按产生它的
来源操作聚类，并按与该操作的关系分层（直接修改 / 登记依赖传播 / 无来源归属），
让 AI 先处理直接修改的簇。

硬边界：聚类只改变呈现顺序，每条待处理记录仍是独立复核义务——行级保留完整
recordId 与状态，簇摘要只汇总数量；来源已撤销的簇显式标注，不冒充已解决；
没有来源操作的记录不强行归簇（unattributed 按所有者分组）。语义判断仍归 AI。

簇与逐条记录是同一结果的两种分页视图（view=clusters / items）。簇行带章节与
所有者清单，真实项目几十个簇就超过单次响应预算，所以 summary 只放计数，簇本身
和逐条记录一样按字节预算分页，不内联进响应元数据。
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re

from .source_impact import ReviewInputs, fragment_review_status, read_review
from .source_model import snapshot_hash
from .source_review import bounded_page, fingerprint, source_snapshot
from .source_maintenance_receipts import load_operation
from .structure_store import StructureError

NEEDS_ATTENTION = {'pending', 'stale', 'conflict', 'needs_information', 'not_checked'}
# 同一分诊结果的两种分页视图：逐条待处理记录，或来源操作簇。
VIEWS = ('items', 'clusters')
# 结构事务回执的 ID 形态；undo 等运行时操作写的 lastOperation 不是这个形态，
# 对应不了任何 journal，按 receipt_missing 呈现而不是崩溃。
OPERATION_ID_PATTERN = re.compile(r'^OP-[0-9A-F]{16}$')


def review_origin(root: Path, state: dict, rid: str, operations: dict) -> dict:
    """Classify the current review against its originating receipt, shared by views."""
    prior = state['items'].get(rid) or {}
    operation_id = prior.get('operationId')
    layer, undone = 'unattributed', False
    if operation_id and not OPERATION_ID_PATTERN.match(str(operation_id)):
        # undo 等运行时操作产生的待处理引用没有可加载的 journal
        operation_id, layer = None, 'receipt_missing'
    elif operation_id:
        if operation_id not in operations:
            operations[operation_id] = load_operation(root, operation_id)
        state_op = operations[operation_id]
        undone = state_op['undone']
        receipt = state_op['receipt']
        changed = set(receipt.get('impact', {}).get('changedIds', ())) if receipt else set()
        layer = 'direct' if rid in changed else 'dependent'
        if receipt is None:
            layer = 'receipt_missing'
    return {'layer': layer, 'operationId': operation_id, 'sourceOperationUndone': undone}


def triage_reviews(root: Path, *, view='items', statuses=None, ownerIds=None, limit=None, maxBytes=None,
                   cursor=None) -> dict:
    if view not in VIEWS:
        raise StructureError('QUERY_VIEW_INVALID', 'triage view must be items or clusters')
    source = source_snapshot(root)
    inputs = ReviewInputs(source)
    state = read_review(root)
    for values in (statuses, ownerIds):
        if values is not None and (not isinstance(values, list) or any(not isinstance(v, str) for v in values)):
            raise StructureError('SOURCE_REVIEW_FILTER_INVALID', 'triage filters must be string lists')
    unknown = set(statuses or ()) - NEEDS_ATTENTION - {'unreviewed'}
    if unknown:
        raise StructureError('SOURCE_REVIEW_FILTER_INVALID', 'unknown triage status', {'unknown': sorted(unknown)})
    wanted = set(statuses) if statuses is not None else (NEEDS_ATTENTION | {'unreviewed'})
    if ownerIds is not None:
        unknown_owners = set(ownerIds) - inputs.index.objects.keys()
        if unknown_owners:
            raise StructureError('SOURCE_REVIEW_FILTER_INVALID', 'unknown owner', {'unknown': sorted(unknown_owners)})

    operations: dict[str, dict] = {}
    cluster_rows: list[dict] = []
    pending_rows: list[dict] = []
    for rid in sorted(inputs.index.fragments):
        fragment = inputs.index.fragments[rid]
        if fragment.get('status') == 'archived' or (ownerIds is not None and fragment['ownerId'] not in ownerIds):
            continue
        input_hash = inputs.fingerprint(rid)
        status = fragment_review_status(inputs, state, rid, input_hash=input_hash)
        if status not in wanted:
            continue
        origin = review_origin(root, state, rid, operations)
        pending_rows.append({
            'recordId': rid, 'ownerId': fragment['ownerId'], 'status': status,
            **origin,
            'inputHash': input_hash,
            'locator': {'fragmentId': rid, 'sourceHash': None, 'offset': 0},
        })

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in pending_rows:
        grouped[(row['operationId'] or '__unattributed__', row['layer'])].append(row)
    for (operation_id, layer), rows in sorted(grouped.items(), key=lambda kv: (kv[0][0] or '', kv[0][1])):
        undone = any(row['sourceOperationUndone'] for row in rows)
        chapter_ids: list[str] = []
        if operation_id and operation_id in operations:
            receipt = operations[operation_id]['receipt']
            chapter_ids = sorted(set(receipt.get('impact', {}).get('approvalInvalidations', ()))) if receipt else []
        cluster_rows.append({
            'operationId': None if operation_id == '__unattributed__' else operation_id,
            'layer': layer,
            'undone': undone,
            'recordCount': len(rows),
            'statuses': sorted({row['status'] for row in rows}),
            'owners': sorted({row['ownerId'] for row in rows}),
            'chapterIds': chapter_ids,
            'note': '来源操作已撤销：复核前先确认当前源内容' if undone else '',
        })
    cluster_index = {(row['operationId'] or '__unattributed__', row['layer']): row for row in cluster_rows}
    for row in pending_rows:
        row['clusterSize'] = cluster_index[(row['operationId'] or '__unattributed__', row['layer'])]['recordCount']
        row.pop('sourceOperationUndone')
    snapshot = {'sourceHash': snapshot_hash(source),
                'reviewHash': fingerprint(state),
                'operationsHash': fingerprint(sorted(operations))}
    return bounded_page(pending_rows if view == 'items' else cluster_rows, snapshot=snapshot,
                        selection={'view': view,
                                   'statuses': sorted(set(statuses)) if statuses is not None else None,
                                   'ownerIds': sorted(set(ownerIds)) if ownerIds is not None else None},
                        code='SOURCE_REVIEW_TRIAGE', limit=limit, maxBytes=maxBytes, cursor=cursor,
                        view=view,
                        summary={'outstandingCount': len(pending_rows),
                                 'clusterCount': len(cluster_rows),
                                 'unattributedCount': sum(1 for row in pending_rows if row['layer'] == 'unattributed')},
                        coverage={'scope': 'outstanding reviews only; resolved and compatible items are not listed',
                                  'clusteringChangesPresentationOrderOnly': True,
                                  'perItemReviewObligationPreserved': True,
                                  'proseScanned': False, 'semanticJudgmentsMadeBy': 'app',
                                  'absenceMeaning': 'an empty triage means no outstanding review state, not that the text is consistent'})
