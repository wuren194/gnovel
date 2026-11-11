"""Established character facets and fixed-weight, replayable reminder samples."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import random

from .project import source_model_policy
from .source_impact import ReviewInputs
from .source_model import SourceIndex, finite_number, snapshot_hash
from .source_review import bounded_page, fingerprint, source_snapshot
from .structure_store import StructureError, utc_now

SAMPLE_FIELDS = {'format', 'sampleId', 'projectId', 'requestKey', 'poolId', 'inputHash',
                 'selectedIds', 'recordedAt', 'receiptHash'}
REMINDER_RULE = '仅作可选提醒；无需逐项表现。未抽中侧面仍然存在，既定事实、知情边界和限制照常完整读取。权重不表示行为概率。'


def validate_profiles(source, index):
    errors = []
    policy = source_model_policy()
    for rid, fragment in index.fragments.items():
        if 'topic' not in fragment:
            continue
        if fragment['topic'] not in policy['profileTopics'] or index.owner_types.get(fragment['ownerId']) != 'character':
            errors.append({'code': 'SOURCE_PROFILE_TOPIC_INVALID', 'path': 'fragments.' + rid + '.topic'})
        if fragment['topic'] == 'state' and not any((fragment.get('validDuring') or {}).values()):
            errors.append({'code': 'SOURCE_PROFILE_STATE_TIME_REQUIRED', 'path': 'fragments.' + rid})
    for pool in source['recallPools']:
        path = 'recallPools.' + pool['poolId']
        if not isinstance(pool['characterId'], str) or index.owner_types.get(pool['characterId']) != 'character':
            errors.append({'code': 'SOURCE_POOL_CHARACTER_INVALID', 'path': path})
        if pool.get('status', 'active') not in policy['recordStatuses']:
            errors.append({'code': 'SOURCE_POOL_STATUS_INVALID', 'path': path})
        facets = pool['facets']
        if not isinstance(facets, list) or not facets:
            errors.append({'code': 'SOURCE_POOL_FACETS_INVALID', 'path': path})
            continue
        if type(pool['count']) is not int or not 1 <= pool['count'] <= len(facets):
            errors.append({'code': 'SOURCE_POOL_COUNT_INVALID', 'path': path})
        seen = set()
        for facet in facets:
            if (not isinstance(facet, dict) or set(facet) != {'fragmentId', 'weight'} or
                    not isinstance(facet['fragmentId'], str) or not finite_number(facet['weight']) or facet['weight'] <= 0):
                errors.append({'code': 'SOURCE_POOL_FACET_INVALID', 'path': path})
                continue
            rid = facet['fragmentId']
            fragment = index.fragments.get(rid)
            if rid in seen:
                errors.append({'code': 'SOURCE_POOL_FACET_DUPLICATE', 'path': path, 'fragmentId': rid})
            seen.add(rid)
            if (not fragment or fragment['ownerId'] != pool['characterId'] or
                    fragment['contentRole'] != 'fact' or fragment.get('topic') not in policy['recallTopics'] or
                    pool.get('status', 'active') == 'active' and fragment.get('status') == 'archived'):
                errors.append({'code': 'SOURCE_POOL_FACET_SCOPE_INVALID', 'path': path, 'fragmentId': rid})
    return errors


def query_profile(root: Path, *, characterId: str, limit: int | None = None,
                  maxBytes: int | None = None, cursor: str | None = None):
    source = source_snapshot(root)
    index = SourceIndex.build(source)
    if not isinstance(characterId, str) or index.owner_types.get(characterId) != 'character':
        raise StructureError('SOURCE_PROFILE_CHARACTER_INVALID', 'select a registered character')
    rows = [deepcopy(row) for row in source['fragments'] if row['ownerId'] == characterId and
            row.get('topic') in source_model_policy()['profileTopics'] and row.get('status') != 'archived']
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'revision': source['revision']},
                        selection={'characterId': characterId}, code='SOURCE_PROFILE',
                        limit=limit, maxBytes=maxBytes, cursor=cursor, characterId=characterId,
                        poolIds=[row['poolId'] for row in source['recallPools'] if row['characterId'] == characterId and
                                 row.get('status', 'active') == 'active'])


def _draw(pool):
    """Weighted sampling without replacement; text and scene never enter selection."""
    remaining = list(pool['facets'])
    generator = random.SystemRandom()
    selected = []
    for _ in range(pool['count']):
        largest = max(row['weight'] for row in remaining)
        weights = [row['weight'] / largest for row in remaining]
        position = generator.choices(range(len(remaining)), weights=weights, k=1)[0]
        selected.append(remaining.pop(position)['fragmentId'])
    return selected


def sample_profile(root: Path, *, poolId: str, requestId: str):
    from .jsonio import read_json
    from .locks import project_lock
    from .source_ids import identity_prefixes
    from .source_requests import request_identity
    from .write_sets import apply_write_set, capture_guards, inventory, json_bytes, safe_project_path, validate_guards
    root = root.resolve()
    with project_lock(root):
        source_guards = capture_guards(root, ['.govel/structure.json'])
        source = source_snapshot(root)
        inputs = ReviewInputs(source)
        pool = next((row for row in source['recallPools'] if row['poolId'] == poolId and
                     row.get('status', 'active') == 'active'), None)
        if pool is None:
            raise StructureError('SOURCE_POOL_NOT_FOUND', 'select an active configured recall pool')
        _, request_key = request_identity(source, requestId)
        sample_id = identity_prefixes()['sample'] + '-' + request_key
        relative = '.govel/profile-samples/' + sample_id + '.json'
        path = safe_project_path(root, relative)
        guards = source_guards + capture_guards(root, [relative])
        input_hash = inputs.fingerprint(poolId)
        replayed = path.exists()
        if replayed:
            receipt = read_json(path)
            if (not isinstance(receipt, dict) or set(receipt) != SAMPLE_FIELDS or
                    receipt['format'] != 'PROFILE_SAMPLE_V1' or receipt['sampleId'] != sample_id or
                    receipt['projectId'] != source['projectId'] or receipt['requestKey'] != request_key or
                    receipt['receiptHash'] != fingerprint({key: value for key, value in receipt.items() if key != 'receiptHash'})):
                raise StructureError('SOURCE_SAMPLE_INVALID', 'saved reminder receipt is unreadable or modified')
            if receipt['poolId'] != poolId:
                raise StructureError('SOURCE_SAMPLE_REQUEST_REUSED', 'requestId already belongs to another recall pool')
            if receipt['inputHash'] != input_hash:
                raise StructureError('SOURCE_SAMPLE_STALE', 'profile inputs changed; this request cannot reroll',
                                     {'sampleId': sample_id, 'instruction': '保留原回执；读取新资料后为新的写作任务创建新请求。'})
            selected = receipt['selectedIds']
            members = {row['fragmentId'] for row in pool['facets']}
            if (not isinstance(selected, list) or len(selected) != pool['count'] or
                    any(not isinstance(rid, str) or rid not in members for rid in selected) or
                    len(set(selected)) != len(selected)):
                raise StructureError('SOURCE_SAMPLE_INVALID', 'receipt selection differs from its configured pool')
            validate_guards(root, guards, stage='profile-sample-replay')
        else:
            receipt = {'format': 'PROFILE_SAMPLE_V1', 'sampleId': sample_id, 'projectId': source['projectId'],
                       'requestKey': request_key, 'poolId': poolId, 'inputHash': input_hash,
                       'selectedIds': _draw(pool), 'recordedAt': utc_now()}
            receipt['receiptHash'] = fingerprint(receipt)
            outputs = {relative: json_bytes(receipt)}
            expected = inventory(root, outputs)
            validate_guards(root, guards, stage='profile-sample-create')
            apply_write_set(root, outputs, expected=expected, operation='source_profile_sample', guards=source_guards)
        rows = [{key: deepcopy(inputs.index.fragments[rid][key]) for key in
                 ('fragmentId', 'ownerId', 'topic', 'text', 'validDuring') if key in inputs.index.fragments[rid]}
                for rid in receipt['selectedIds']]
        return {'ok': True, 'code': 'SOURCE_PROFILE_SAMPLE', 'sampleId': sample_id, 'poolId': poolId,
                'characterId': pool['characterId'], 'replayed': replayed, 'facets': rows,
                'instruction': REMINDER_RULE, 'grantsApproval': False, 'changesPersonality': False}


def task_source_actions(source, chapter, task):
    """Advertise explicit queries; task preparation stays read-only."""
    from .source_plan import chapter_plan
    plan = chapter_plan(source, chapter['chapterId'])
    referenced = {row['targetId'] for row in plan['recipe']['references']}
    characters = {row['entityId'] for row in source['entities'] if row['entityType'] == 'character' and
                  row['entityId'] in referenced}
    pools = [row for row in source['recallPools'] if row['characterId'] in characters and
             row.get('status', 'active') == 'active']
    recall = [{'operation': 'source.profile-sample', 'arguments': {'poolId': row['poolId'],
               'requestId': ':'.join(('profile', chapter['chapterId'], task, plan['sourcePlanHash'],
                                      row['poolId'], fingerprint(row)))}}
              for row in pools]
    cognition = [{'operation': 'source.cognition', 'arguments': {'holderId': row['cognition']['holderId'],
                  'targetId': row['cognition']['targetId'], 'asOf': chapter.get('storyTimeStartId')}}
                 for row in source['fragments'] if row.get('cognition') and
                 row['cognition']['holderId'] in characters and row.get('status') != 'archived']
    unique = {fingerprint(row): row for row in cognition}
    return {'profileRecall': recall, 'cognitionQueries': list(unique.values())}
