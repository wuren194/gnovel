"""Model/proof equivalence, actual transport and adversarial synthetic mutations."""
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core import context_builder as builder
from scripts.core.artifacts import sha256_json
from scripts.core.context_health import assess_context
from scripts.core.context_transport import semantic_view, task_model_response
from scripts.core.structure_store import StructureError
from scripts.mcp_server import handle
from tests.context_semantic_fixture import fixture, fixed_transport_sample
from tests.test_context_builder import inventory, save_context_source


def assert_semantics(case, original, model):
    """Independent oracle over retained source records, not projection equality."""
    expected = {json.dumps({'kind': row['field'], 'content': row['value']}, ensure_ascii=False, sort_keys=True)
                for tier in ('working', 'semantic', 'episodic') for row in original['memoryPack'][tier]}
    actual = [json.dumps({'kind': row['kind'], 'content': row['content']}, ensure_ascii=False, sort_keys=True)
              for row in model['memories']]
    case.assertEqual(expected, set(actual))
    case.assertEqual(len(actual), len(set(actual)), 'each retained semantic record appears once')
    case.assertEqual(model['task'], original['task'])
    case.assertEqual(model['pov'], original.get('povCharacter'))
    case.assertEqual([row['query'] for row in model['sourceReads']],
                     [original['sourceContext'][key] for key in ('contentQuery', 'planQuery', 'rulesQuery')])
    case.assertEqual([row['statement'] for row in model['authorDecisions']],
                     [row['statement'] for row in original.get('authorDecisions', [])])
    case.assertEqual([row['excerpt'] for row in model['referenceKnowledge']],
                     [row['excerpt'] for row in original.get('knowledgePack', {}).get('items', [])])
    for row in model['referenceKnowledge']:
        case.assertEqual(row['role'], 'REFERENCE KNOWLEDGE')
    prose = [{k: v for k, v in row.items() if k != 'handle'} for row in model['recentProseReads']]
    case.assertEqual(prose, original['recentChapters'])
    case.assertEqual(len(prose), len({json.dumps(row, sort_keys=True) for row in prose}))
    case.assertEqual(model.get('workVoiceProfile'), original.get('workVoiceProfile'))


class SemanticViewTests(unittest.TestCase):
    def setUp(self):
        from tests.fixtures_knowledge import staged_reference
        self.enterContext(staged_reference(canonical=True))
        temp = tempfile.TemporaryDirectory(prefix='round3-context-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def build(self, size='small', task='planner', **kwargs):
        chapter, goal = fixture(self.root, size)
        return builder.build_station_context(self.root, chapter, task_type=task, chapter_goal=goal,
                                             pov_character='character-ALPHA', view='station', read_only=True, **kwargs)

    def test_all_sizes_and_stations_preserve_semantics_and_machine_evidence(self):
        for size in ('small', 'medium', 'edge'):
            for task in ('planner', 'writer', 'editor'):
                with self.subTest(size=size, task=task):
                    ctx = self.build(size, task)
                    before = deepcopy(ctx)
                    disk = inventory(self.root)
                    model, proof = semantic_view(ctx)
                    assert_semantics(self, ctx, model)
                    self.assertEqual(ctx, before)
                    self.assertEqual(disk, inventory(self.root))
                    for key in ('dependencyManifest', 'contextHash', 'filterProvenance', 'readiness',
                                'sourceContext', 'layers', 'knowledgeRoute', 'contextBudget'):
                        self.assertEqual(proof[key], ctx[key])
                    for row in model['memories']:
                        mapping = proof['handles'][row['handle']]
                        self.assertEqual(mapping['valueHash'], sha256_json(row['content']))
                        for _, tier, index in mapping['locations']:
                            self.assertEqual(ctx['memoryPack'][tier][index]['value'], row['content'])
                    for row in model['referenceKnowledge']:
                        mapping = proof['handles'][row['handle']]
                        original = next(item for item in ctx['knowledgePack']['items'] if item['assetId'] == mapping['assetId'])
                        self.assertEqual(mapping['sha256'], original['sha256'])
                        self.assertNotIn('excerpt', proof['knowledgePack']['items'][mapping['locations'][0][-1]])
                    self.assertLess(len(builder.serialize_context(model)), len(builder.serialize_context(ctx)))
                    self.assertLessEqual(len(builder.serialize_context(model)), ctx['contextBudget']['total'])
                    self.assertEqual((model, proof), semantic_view(ctx))
                    for forbidden in ('dependencyManifest', 'contextHash', 'sha256', 'contentHash', 'generation'):
                        self.assertNotIn('"' + forbidden + '"', builder.serialize_context(model))

    def test_fixed_neutral_payload_and_live_fixture_are_deterministic(self):
        for task in ('planner', 'writer', 'editor'):
            ctx = fixed_transport_sample(task)
            model, proof = semantic_view(ctx)
            assert_semantics(self, ctx, model)
            self.assertEqual(len(model['memories']), 1)
            self.assertEqual(len(proof['handles']['MEM-1']['locations']), 2)
            self.assertEqual(model['referenceKnowledge'][0]['excerpt'],
                             '先让借书条参与日常整理，再让来客辨认字迹。')
            self.assertNotIn('excerpt', proof['knowledgePack']['items'][0])
            self.assertEqual(proof['dependencyManifest'], ctx['dependencyManifest'])
            self.assertEqual(proof['contextHash'], ctx['contextHash'])
            self.assertEqual((model, proof), semantic_view(json.loads(json.dumps(ctx))))
        self.assertEqual(self.build('medium'), self.build('medium'))

    def test_mutant_A_missing_proof_hash_is_rejected(self):
        ctx = self.build()
        del ctx['dependencyManifest']['sourceHash']
        self.assertFalse(assess_context(ctx)['ready'])
        with self.assertRaises(StructureError):
            semantic_view(ctx)

    def test_mutant_B_missing_canon_fact_is_rejected(self):
        ctx = self.build()
        model, _ = semantic_view(ctx)
        model['memories'] = [row for row in model['memories'] if row['kind'] != 'entity']
        with self.assertRaises(AssertionError):
            assert_semantics(self, ctx, model)

    def test_mutant_C_duplicate_prose_is_rejected(self):
        ctx = self.build()
        model, _ = semantic_view(ctx)
        model['recentProseReads'].append(deepcopy(model['recentProseReads'][0]))
        with self.assertRaises(AssertionError):
            assert_semantics(self, ctx, model)

    def test_mutant_D_knowledge_as_canon_is_rejected(self):
        ctx = self.build()
        model, _ = semantic_view(ctx)
        self.assertTrue(model['referenceKnowledge'])
        model['referenceKnowledge'][0]['role'] = 'CANON FACTS'
        with self.assertRaises(AssertionError):
            assert_semantics(self, ctx, model)

    def test_hash_tampering_and_blocked_pov_fail_closed(self):
        ctx = self.build()
        ctx['memoryPack']['working'][0]['value']['text'] = 'changed'
        with self.assertRaises(StructureError) as caught:
            semantic_view(ctx)
        self.assertEqual(caught.exception.code, 'CONTEXT_PROOF_INVALID')
        ctx = self.build()
        ctx['readiness']['ready'] = False
        ctx['readiness']['reasons'] = ['POV_HARD_CONSTRAINTS_UNAVAILABLE']
        with self.assertRaises(StructureError):
            semantic_view(ctx)

    def test_unknown_field_is_retained_and_budget_is_fail_closed(self):
        ctx = self.build()
        ctx['futureField'] = {'unknownFact': '未分类信息必须保留'}
        ctx['contextHash'] = sha256_json({k: v for k, v in ctx.items() if k != 'contextHash'})
        self.assertEqual(semantic_view(ctx)[0]['unclassifiedContext']['futureField'], ctx['futureField'])
        ctx['contextBudget']['total'] = 1
        ctx['contextBudget']['usedCharacters'] = 1
        ctx['contextHash'] = sha256_json({k: v for k, v in ctx.items() if k != 'contextHash'})
        with self.assertRaises(StructureError):
            semantic_view(ctx)

    def test_formatting_does_not_change_legacy_hash_and_source_change_is_stale(self):
        ctx = self.build('edge')
        self.assertEqual(semantic_view(json.loads(json.dumps(ctx)))[0], semantic_view(ctx)[0])
        chapter, goal = fixture(self.root, 'edge')
        source = json.loads((self.root / '.govel/structure.json').read_text())
        source['entities'][0]['summary'] += '依赖已变化。'
        save_context_source(self.root, source)
        new = builder.build_station_context(self.root, chapter, chapter_goal=goal, pov_character='character-ALPHA',
                                            view='station', read_only=True)
        self.assertFalse(assess_context(new, expectedHash=ctx['contextHash'])['ready'])

    def test_actual_mcp_default_text_and_structured_content_exclude_proof(self):
        for task in ('planner', 'writer', 'editor'):
            chapter, goal = fixture(self.root, 'medium')
            args = {'projectRoot': str(self.root), 'chapterId': chapter, 'taskType': task,
                    'chapterGoal': goal, 'povCharacter': 'character-ALPHA'}
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': 'read_continuation_context', 'arguments': args}}
            result = handle(request)['result']
            self.assertFalse(result['isError'])
            model = result['structuredContent']
            text = result['content'][0]['text']
            self.assertEqual(model['view'], 'model')
            self.assertNotIn('dependencyManifest', text)
            self.assertNotIn('dependencyManifest', model)
            self.assertIn('CONTEXT_MODEL', text)
            from scripts.core.transport_cache import get
            proof = get(self.root, result['_meta']['contextProofRef'], kind='context-proof')
            self.assertTrue(proof['dependencyManifest']['sourceHash'])
            ctx = builder.build_station_context(self.root, chapter, task_type=task, chapter_goal=goal,
                                                pov_character='character-ALPHA', view='station', read_only=True)
            assert_semantics(self, ctx, model['modelContext'])
            request['params']['arguments']['view'] = 'station'
            debug = handle(request)['result']
            self.assertEqual(debug['structuredContent']['contextHash'], ctx['contextHash'])
            self.assertIn('dependencyManifest', json.loads(debug['content'][0]['text']))

    def test_prepare_model_reads_include_editor_draft_and_contracts_without_hashes(self):
        ctx = self.build(task='editor')
        model, _ = semantic_view(ctx)
        report = {'ok': True, 'code': 'TASK_READY', 'task': 'editor', 'readiness': {'taskReady': True},
                  'context': {'modelContext': model}, 'readList': [
                      {'path': '工作台/候选.md', 'required': True, 'reason': '当前稿件', 'sha256': 'a'*64},
                      {'path': '正文/甲—城门🌙.md', 'required': True, 'reason': '前情', 'sha256': 'b'*64}],
                  'promptPlan': {'contracts': [{'path': 'contract.md', 'readRanges': [
                      {'startLine': 1, 'endLine': 10, 'sha256': 'c'*64}]}]}}
        presentation = task_model_response(report)
        self.assertEqual(presentation['readList'][0]['path'], '工作台/候选.md')
        self.assertNotIn('path', presentation['readList'][1])
        self.assertEqual(presentation['readList'][1]['contextHandle'], 'PROSE-1')
        self.assertNotIn('sha256', builder.serialize_context(presentation))

    def test_cli_model_views_and_explicit_mcp_model_match_default(self):
        from tests.test_context_transport import run_cli
        chapter, goal = fixture(self.root, 'small')
        for task in ('planner', 'writer', 'editor'):
            cli = run_cli('task', 'context', '--project-root', str(self.root), '--chapter-id', chapter,
                          '--task-type', task, '--pov-character', 'character-ALPHA', '--chapter-goal', goal, '--view', 'model')
            direct_cli = run_cli('context', '--project-root', str(self.root), '--chapter-id', chapter,
                                 '--task-type', task, '--pov-character', 'character-ALPHA', '--chapter-goal', goal, '--view', 'model')
            args = {'projectRoot': str(self.root), 'chapterId': chapter, 'taskType': task,
                    'chapterGoal': goal, 'povCharacter': 'character-ALPHA', 'view': 'model'}
            result = handle({'id': 1, 'method': 'tools/call', 'params': {
                'name': 'read_continuation_context', 'arguments': args}})['result']
            self.assertEqual(cli, direct_cli)
            self.assertEqual(cli, result['structuredContent'])
            self.assertIn('contextProofRef', result['_meta'])
        prepare_args = {'projectRoot': str(self.root), 'chapterId': chapter, 'task': 'planner',
                        'chapterGoal': goal, 'povCharacter': 'character-ALPHA', 'view': 'model'}
        result = handle({'id': 1, 'method': 'tools/call', 'params': {
            'name': 'prepare_task', 'arguments': prepare_args}})['result']
        self.assertFalse(result['isError'])
        self.assertIn('contextProofRef', result['_meta'])
        prepared = run_cli('prepare', '--project-root', str(self.root), '--chapter-id', chapter,
                           '--task', 'planner', '--chapter-goal', goal, '--pov-character', 'character-ALPHA', '--view', 'model')
        self.assertEqual(prepared['modelContext'], result['structuredContent']['modelContext'])

    def test_discovered_reads_preserve_model_view_and_private_proof(self):
        from scripts.core.transport_cache import get
        chapter, goal = fixture(self.root, 'small')
        base = {'chapterId': chapter, 'chapterGoal': goal, 'povCharacter': 'character-ALPHA'}
        for operation, name, extra in (('context.read', 'read_continuation_context', {'taskType': 'planner'}),
                                        ('task.prepare', 'prepare_task', {'task': 'planner'})):
            for view in (None, 'model', 'station'):
                with self.subTest(operation=operation, view=view):
                    arguments = {**base, **extra, **({'view': view} if view else {})}
                    direct = handle({'id': 1, 'method': 'tools/call', 'params': {
                        'name': name, 'arguments': {'projectRoot': str(self.root), **arguments}}})['result']
                    bridged = handle({'id': 1, 'method': 'tools/call', 'params': {
                        'name': 'read_discovered_operation', 'arguments': {
                            'projectRoot': str(self.root), 'operation': operation, 'arguments': arguments}}})['result']
                    self.assertFalse(direct['isError'], direct)
                    self.assertFalse(bridged['isError'], bridged)
                    payload, expected = bridged['structuredContent'], direct['structuredContent']
                    self.assertEqual(payload['code'], expected['code'])
                    if view != 'station':
                        self.assertEqual(payload['modelContext'], expected['modelContext'])
                        proof = get(self.root, bridged['_meta']['contextProofRef'], kind='context-proof')
                        self.assertEqual(proof, get(self.root, direct['_meta']['contextProofRef'], kind='context-proof'))
                        self.assertNotIn('modelProof', bridged['content'][0]['text'])
                    else:
                        self.assertNotIn('_meta', bridged)
                        self.assertEqual(payload.get('context', payload).get('contextHash'),
                                         expected.get('context', expected).get('contextHash'))

    def test_author_decision_and_voice_content_are_not_lost(self):
        from tests.test_context_builder import ContextBuilderTests, build
        from scripts.core.work_voice_profile import AXES, preview_voice_profile
        from scripts.core.structure_commands import commit_plan
        from scripts.core.structure_store import load_structure
        chapter, goal = fixture(self.root, 'small')
        helper = ContextBuilderTests()
        helper.root = self.root
        helper.approve_decision(statement='本章保留秘密', rationale='作者指定节奏', scopeIds=[chapter], dependsOn=[])
        for action in ('set', 'approve'):
            extra = ({'axes': {key: '适度变化' for key in AXES}, 'examples': ['以行动写出戒备'], 'applicability': '全书'}
                     if action == 'set' else {'approvalActor': 'author', 'approvalEvidence': '已审阅具体版本'})
            plan = preview_voice_profile(self.root, action=action, expectedRevision=load_structure(self.root)['revision'],
                                         reason='测试作品声纹', **extra)
            commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        for task in ('planner', 'writer', 'editor'):
            ctx = build(self.root, chapter_goal=goal, task_type=task)
            model, proof = semantic_view(ctx)
            assert_semantics(self, ctx, model)
            self.assertEqual(model['authorDecisions'][0]['statement'], '本章保留秘密')
            self.assertNotIn('statement', proof['authorDecisions'][0])
            self.assertEqual(model['workVoiceProfile']['examples'], ['以行动写出戒备'])
            self.assertNotIn('examples', proof['workVoiceProfile'])

    def test_optional_prose_absence_and_empty_knowledge_are_preserved(self):
        ctx = self.build('edge')
        (self.root / '正文/甲—城门🌙.md').unlink()
        absent = builder.build_station_context(self.root, 'chapter-AAA', task_type='writer',
                                               chapter_goal='completely-unrelated-query', pov_character='character-ALPHA',
                                               view='station', read_only=True)
        model, proof = semantic_view(absent)
        self.assertEqual(model['referenceKnowledge'], [])
        self.assertFalse(model['recentProseReads'][0]['exists'])
        self.assertIsNone(proof['handles']['PROSE-1']['sha256'])
        self.assertFalse(assess_context(absent, expectedHash=ctx['contextHash'])['ready'])


if __name__ == '__main__':
    unittest.main()
