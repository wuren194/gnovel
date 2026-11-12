"""Names select candidates; only stable identities bind authored changes."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.canon_proposals import preview_canon_change, resolve_canon_target
from scripts.core.catalog_commands import preview_create_entity
from scripts.core.chapter_contract import source_contract
from scripts.core.commands import get_entity
from scripts.core.entity_registry import resolve_entity
from scripts.core.jsonio import write_json
from scripts.core.query import query_project
from scripts.core.refactor import preview_alias_entity, preview_rename_entity, preview_archive_entity, preview_restore_entity
from scripts.core.source_changes import preview_source_change
from scripts.core.source_fields import query_settings_source
from scripts.core.source_relation_proposals import resolve_relation_target
from scripts.core.source_search import search_source
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.task_prepare import prepare_task
from tests.test_source_plan import planning_source


class EntityResolutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.source, self.chapter = planning_source(self.root)
        self.alpha, self.beta = self.source['entities'][:2]
        self.cid = self.chapter['chapterId']
        self.source['references'] = [
            {'sourceId': self.cid, 'targetId': self.alpha['entityId'], 'kind': 'uses'}]

    def save(self):
        write_json(self.root / '.govel/structure.json', self.source)

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan['operationId'],
                           plan_hash=plan['planHash'], approved=True)

    def twins(self):
        self.alpha.update(name='林舟', aliases=['守门人'], summary='东门守卫。')
        self.beta.update(name='林舟', aliases=['守门人'], summary='西门守卫。')
        self.save()

    def test_full_id_precedes_another_entity_alias_at_read_and_write_boundaries(self):
        self.beta['aliases'] = [self.alpha['entityId']]
        self.save()
        resolved = resolve_canon_target(self.root, selector=self.alpha['entityId'])
        self.assertEqual(resolved['status'], 'resolved')
        self.assertEqual(resolved['matches'][0]['entityId'], self.alpha['entityId'])
        self.assertEqual(get_entity(self.root, self.alpha['entityId'])['entity']['entityId'], self.alpha['entityId'])
        plan = preview_alias_entity(self.root, selector=self.alpha['entityId'], aliases=['东门人'])
        self.commit(plan)
        current = load_structure(self.root)
        self.assertEqual(current['entities'][0]['aliases'], ['东门人'])
        self.assertEqual(current['entities'][1], self.beta)

    def test_id_case_and_whitespace_are_not_name_normalization(self):
        self.alpha['name'] = ' River Keeper '
        self.beta['aliases'] = ['character-alpha', 'character-MISSING']
        self.save()
        self.assertEqual(resolve_entity(self.source, ' river KEEPER ')['entityId'], self.alpha['entityId'])
        for selector in ('character-alpha', 'CHARACTER-ALPHA', ' character-ALPHA ', 'character-MISSING'):
            with self.subTest(selector=selector):
                result = resolve_canon_target(self.root, selector=selector)
                self.assertEqual(result['status'], 'not_found')
                self.assertNotIn('sessionRef', result)
                with self.assertRaises(StructureError) as caught:
                    resolve_entity(self.source, selector)
                self.assertEqual(caught.exception.code, 'ENTITY_NOT_FOUND')

    def test_same_names_and_shared_aliases_return_distinguishable_candidates(self):
        self.twins()
        for selector in ('林舟', ' 守门人 '):
            result = resolve_canon_target(self.root, selector=selector)
            self.assertEqual(result['status'], 'ambiguous')
            self.assertNotIn('sessionRef', result)
            self.assertNotIn('recordHash', result)
            self.assertEqual({row['entityId'] for row in result['matches']},
                             {self.alpha['entityId'], self.beta['entityId']})
            for row in result['matches']:
                self.assertEqual(row['entityType'], 'character')
                self.assertEqual(row['name'], '林舟')
                self.assertTrue(row['summary'])
            self.assertEqual(result['matches'][0]['chapterIds'], [self.cid])
            with self.assertRaises(StructureError) as caught:
                get_entity(self.root, selector)
            self.assertEqual(caught.exception.code, 'ENTITY_AMBIGUOUS')
            self.assertEqual(caught.exception.details['candidates'], result['matches'])

    def test_type_chapter_and_archive_scope_apply_before_resolution(self):
        self.twins()
        self.source['entities'].append({'entityId': 'prop-GATE', 'entityType': 'prop',
            'name': '林舟', 'aliases': [self.beta['entityId']], 'status': 'active'})
        self.save()
        for options, expected in (({'entityType': 'prop'}, 'prop-GATE'),
                                  ({'chapterId': self.cid}, self.alpha['entityId'])):
            result = resolve_canon_target(self.root, selector='林舟', **options)
            self.assertEqual(result['matches'][0]['entityId'], expected)
            self.assertIn('sessionRef', result)
        for selector, options in ((self.beta['entityId'], {'chapterId': self.cid}),
                                  (self.beta['entityId'], {'entityType': 'prop'})):
            self.assertEqual(resolve_canon_target(self.root, selector=selector, **options)['status'], 'not_found')
        self.alpha['status'] = 'archived'
        self.beta['aliases'].append(self.alpha['entityId'])
        self.save()
        self.assertEqual(resolve_canon_target(self.root, selector=self.alpha['entityId'])['status'], 'not_found')
        archived = resolve_canon_target(self.root, selector=self.alpha['entityId'], includeArchived=True)
        self.assertEqual(archived['status'], 'resolved')
        self.assertNotIn('sessionRef', archived)

    def test_ambiguous_modification_does_not_write_a_plan(self):
        self.twins()
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for change in (lambda: preview_alias_entity(self.root, selector='林舟', aliases=['新称']),
                       lambda: preview_rename_entity(self.root, selector='守门人', new_name='新名',
                                                     keep_old_name_as_alias=True)):
            with self.assertRaises(StructureError) as caught:
                change()
            self.assertEqual(caught.exception.code, 'ENTITY_AMBIGUOUS')
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_explicit_rename_preserves_references_and_expires_old_binding(self):
        self.source['relations'] = [{'relationId': 'relation-GUARDS', 'fromEntity': self.alpha['entityId'],
                                    'toEntity': self.beta['entityId'], 'kind': 'knows'}]
        self.save()
        resolved = resolve_canon_target(self.root, selector=self.alpha['entityId'])
        relation = resolve_relation_target(self.root, relationId='relation-GUARDS')
        plan = preview_canon_change(self.root, sessionRef=resolved['sessionRef'], fieldPath='name',
            expectedOldExists=True, expectedOldValue=self.alpha['name'], newValue=self.beta['name'],
            reason='作者确认同名人物', evidence=[{'fragmentId': 'fact-LIFE', 'quote': '他独自守过城门。'}],
            approvalActor='author', approvalEvidence='采用此姓名')
        self.commit(plan)
        current = load_structure(self.root)
        self.assertEqual(current['references'], self.source['references'])
        self.assertEqual(current['relations'], self.source['relations'])
        self.assertEqual(current['attributes'], self.source['attributes'])
        self.assertEqual(current['entities'][1:], self.source['entities'][1:])
        self.assertEqual(current['entities'][0]['entityId'], self.alpha['entityId'])
        self.assertEqual(resolve_canon_target(self.root, selector=self.beta['name'])['status'], 'ambiguous')
        with self.assertRaises(StructureError) as caught:
            preview_canon_change(self.root, sessionRef=resolved['sessionRef'], fieldPath='name',
                expectedOldExists=True, expectedOldValue=self.beta['name'], newValue='新名', reason='旧绑定',
                evidence=[], approvalActor='author', approvalEvidence='确认')
        self.assertEqual(caught.exception.code, 'SESSION_REF_STALE')
        self.assertNotEqual(resolve_relation_target(self.root, relationId='relation-GUARDS')['sessionRef'],
                            relation['sessionRef'])

    def test_creation_allows_same_and_cross_type_names_with_shared_aliases(self):
        self.alpha['aliases'] = ['守门人']
        self.save()
        created = []
        for kind in ('character', 'prop'):
            plan = preview_create_entity(self.root, entity_type=kind, name=self.alpha['name'], aliases=['守门人'])
            self.commit(plan)
            created.append(load_structure(self.root)['entities'][-1]['entityId'])
        self.assertEqual(len(set(created + [self.alpha['entityId']])), 3)
        self.assertEqual(len(resolve_canon_target(self.root, selector='守门人')['matches']), 3)

    def test_settings_and_dimension_queries_resolve_inside_their_filters(self):
        self.source['entities'].append({'entityId': 'prop-GATE', 'entityType': 'prop',
                                       'name': self.alpha['name'], 'status': 'active'})
        self.save()
        settings = query_settings_source(self.root, self.source, selector=self.alpha['name'], kind='prop')
        self.assertEqual([r['entityId'] for r in settings['matches']], ['prop-GATE'])
        self.beta['name'] = self.alpha['name']
        self.save()
        for options in ({'kind': 'prop'}, {'referencedBy': self.cid}):
            result = query_project(self.root, 'nodes', selector=self.alpha['name'], **options)
            self.assertEqual(result['totalMatching'], 1)
        with self.assertRaises(StructureError) as caught:
            query_project(self.root, 'nodes', selector=self.alpha['name'], kind='character')
        self.assertEqual(caught.exception.code, 'ENTITY_AMBIGUOUS')

    def test_timeline_problem_and_status_filters_narrow_same_names(self):
        self.source['entities'][2]['name'] = '交班'
        self.source['entities'][4]['name'] = '交班'
        self.save()
        result = query_project(self.root, 'timeline', selector='交班', problems=True)
        self.assertEqual([row['id'] for row in result['items']], ['time-UNKNOWN'])
        self.twins()
        self.beta['status'] = 'archived'
        self.save()
        result = query_project(self.root, 'nodes', selector='林舟', status='archived')
        self.assertEqual([row['id'] for row in result['items']], [self.beta['entityId']])

    def test_prepare_never_overwrites_an_ambiguous_name(self):
        self.twins()
        report = prepare_task(self.root, 'titler', chapter_id=self.cid, entities=['林舟'])
        issue = next(row for row in report['missing'] if row['code'] == 'ENTITY_AMBIGUOUS')
        self.assertTrue(issue['blocking'])
        self.assertEqual({r['entityId'] for r in issue['details']['candidates']},
                         {self.alpha['entityId'], self.beta['entityId']})
        self.assertNotIn(self.beta['entityId'], report['scope']['entities'])
        self.assertFalse(report['readiness']['scopeReady'])
        self.assertFalse(report['actionReadiness']['executable'])

    def test_chapter_pov_resolves_id_before_alias_and_rejects_same_name(self):
        self.beta['aliases'] = [self.alpha['entityId']]
        fragment = self.source['fragments'][-1]
        fragment['chapterContract']['pov'] = self.alpha['entityId']
        self.save()
        self.assertEqual(source_contract(self.source, self.cid)['pov'], self.alpha['entityId'])
        self.twins()
        fragment.pop('chapterContract')
        fragment['text'] = '# 本章定位\n| POV | 林舟 |\n| 字数 | 100-200字 |'
        report = source_contract(self.source, self.cid)
        self.assertIsNone(report['pov'])
        self.assertIn('CHAPTER_CONTRACT_POV_AMBIGUOUS', report['issues'])
        self.assertEqual(len(report['povResolution']['matches']), 2)
        self.save()
        with patch('scripts.core.task_prepare._context', side_effect=AssertionError('unresolved POV must not build author context')):
            prepared = prepare_task(self.root, 'planner', chapter_id=self.cid)
        self.assertFalse(prepared['readiness']['contextReady'])
        self.assertIn('CHAPTER_CONTRACT_POV_AMBIGUOUS', [row['code'] for row in prepared['missing']])
        self.assertNotIn('modelContext', prepared['context'])

    def test_search_resolves_only_visible_names_and_ids_in_selection(self):
        self.twins()
        self.beta['aliases'].append(self.alpha['entityId'])
        self.save()
        with self.assertRaises(StructureError) as caught:
            search_source(self.root, terms=['他'], ownerSelectors=['林舟'])
        self.assertEqual(caught.exception.code, 'SOURCE_SEARCH_OWNER_AMBIGUOUS')
        self.assertEqual(len(caught.exception.details['candidates']), 2)
        result = search_source(self.root, terms=['他'], ownerSelectors=[self.alpha['entityId']])
        self.assertTrue(result['items'])
        with self.assertRaises(StructureError) as caught:
            search_source(self.root, terms=['他'], selection={'ownerIds': [self.beta['entityId']]},
                          ownerSelectors=[self.alpha['entityId']])
        self.assertEqual(caught.exception.code, 'SOURCE_SEARCH_OWNER_NOT_FOUND')

    def test_unicode_casefold_is_shared_by_name_consumers(self):
        self.alpha['name'] = 'Straße'
        self.alpha['aliases'] = ['RIVER KEEPER']
        self.save()
        for selector in (' STRASSE ', ' river keeper '):
            self.assertEqual(resolve_entity(self.source, selector)['entityId'], self.alpha['entityId'])
            self.assertEqual(resolve_canon_target(self.root, selector=selector)['status'], 'resolved')
            self.assertEqual(search_source(self.root, terms=['他'], ownerSelectors=[selector])['ownerResolution']['ownerIds'],
                             [self.alpha['entityId']])
        self.beta['name'] = 'STRASSE'
        self.save()
        self.assertEqual(resolve_canon_target(self.root, selector='straße')['status'], 'ambiguous')

    def test_case_distinct_permanent_ids_keep_separate_identities(self):
        self.source['entities'].append({'entityId': 'character-alpha', 'entityType': 'character',
                                       'name': '人物小甲', 'aliases': [self.alpha['entityId']]})
        self.save()
        for identity in ('character-alpha', 'character-ALPHA'):
            result = resolve_canon_target(self.root, selector=identity)
            self.assertEqual([r['entityId'] for r in result['matches']], [identity])

    def test_perspective_shared_designations_do_not_disclose_author_metadata(self):
        for entity, binding in ((self.alpha, 'attribute-LIFE'), (self.beta, 'attribute-BETA-HEIGHT')):
            entity.update(name='PRIVATE-' + entity['entityId'], aliases=['PRIVATE-alias'], summary='PRIVATE-summary',
                          designations=[{'label': '守门人', 'perspectiveIds': [self.alpha['entityId']], 'validDuring': {}}])
            self.source['fragments'].append({'fragmentId': 'belief-' + entity['entityId'], 'ownerId': entity['entityId'],
                'contentRole': 'belief', 'perspectiveId': self.alpha['entityId'], 'text': '那人带着钥匙。',
                'order': 10, 'contentBindings': [binding]})
        self.save()
        selection = {'knowledgeMode': 'perspective', 'perspectiveIds': [self.alpha['entityId']]}
        with self.assertRaises(StructureError) as caught:
            search_source(self.root, terms=['钥匙'], selection=selection, ownerSelectors=['守门人'])
        self.assertEqual(caught.exception.code, 'SOURCE_SEARCH_OWNER_AMBIGUOUS')
        self.assertEqual({r['label'] for r in caught.exception.details['candidates']}, {'守门人'})
        self.assertNotIn('PRIVATE', json.dumps(caught.exception.details))
        for selector in ('PRIVATE-alias', self.alpha['entityId'].upper()):
            with self.assertRaises(StructureError) as caught:
                search_source(self.root, terms=['钥匙'], selection=selection, ownerSelectors=[selector])
            self.assertEqual(caught.exception.code, 'SOURCE_SEARCH_OWNER_NOT_FOUND')
        result = search_source(self.root, terms=['钥匙'], selection=selection, ownerSelectors=[self.beta['entityId']])
        self.assertEqual(result['ownerResolution']['ownerIds'], [self.beta['entityId']])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_archive_name_does_not_replace_explicit_id_and_restore_keeps_identity(self):
        self.twins()
        self.commit(preview_archive_entity(self.root, selector=self.beta['entityId']))
        current = resolve_canon_target(self.root, selector='林舟')
        self.assertEqual(current['matches'][0]['entityId'], self.alpha['entityId'])
        self.assertIn('sessionRef', current)
        archived = resolve_canon_target(self.root, selector='林舟', includeArchived=True)
        self.assertEqual(archived['status'], 'ambiguous')
        self.assertNotIn('sessionRef', archived)
        self.commit(preview_restore_entity(self.root, selector=self.beta['entityId']))
        self.assertEqual(resolve_canon_target(self.root, selector='林舟')['status'], 'ambiguous')

    def test_alias_proposal_may_share_a_name_without_changing_other_entity(self):
        resolved = resolve_canon_target(self.root, selector=self.alpha['entityId'])
        plan = preview_canon_change(self.root, sessionRef=resolved['sessionRef'], fieldPath='aliases',
            expectedOldExists=True, expectedOldValue=[], newValue=[self.beta['name']],
            reason='登记共同称呼', evidence=[{'fragmentId': 'fact-LIFE', 'quote': '他独自守过城门。'}],
            approvalActor='author', approvalEvidence='确认称呼')
        self.commit(plan)
        self.assertEqual(load_structure(self.root)['entities'][1], self.beta)
        self.assertEqual(resolve_canon_target(self.root, selector=self.beta['name'])['status'], 'ambiguous')

    def test_batch_creation_uses_handles_to_keep_same_name_endpoints_distinct(self):
        changes = [{'action': 'put', 'collection': 'entities', 'record':
                    {'entityId': handle, 'entityType': 'character', 'name': '守门人', 'aliases': ['小林']}}
                   for handle in ('$east', '$west')]
        changes.append({'action': 'put', 'collection': 'relations', 'record':
                        {'fromEntity': '$east', 'toEntity': '$west', 'kind': '轮值'}})
        plan = preview_source_change(self.root, changes=changes, requestId='same-name',
                                     baseRevision=self.source['revision'], reason='两位同名守卫')
        self.commit(plan)
        current = load_structure(self.root)
        east, west = current['entities'][-2:]
        self.assertNotEqual(east['entityId'], west['entityId'])
        relation = current['relations'][0]
        self.assertEqual((relation['fromEntity'], relation['toEntity']), (east['entityId'], west['entityId']))
        self.assertEqual(resolve_relation_target(self.root, fromEntity='守门人', toEntity='守门人', kind='轮值')['status'], 'not_found')
        resolved = resolve_relation_target(self.root, fromEntity=east['entityId'], toEntity=west['entityId'], kind='轮值')
        self.assertEqual(resolved['status'], 'resolved')
        self.assertIn('sessionRef', resolved)

    def test_task_scope_keeps_arc_and_entity_names_ambiguous_but_ids_exact(self):
        arc = self.source['arcs'][0]
        self.alpha['name'] = arc['title']
        self.beta['aliases'] = [arc['arcId']]
        self.save()
        report = prepare_task(self.root, 'titler', chapter_id=self.cid, entities=[arc['title']])
        issue = next(row for row in report['missing'] if row['code'] == 'ENTITY_AMBIGUOUS')
        self.assertEqual(len(issue['details']['candidates']), 2)
        report = prepare_task(self.root, 'titler', chapter_id=self.cid, entities=[arc['arcId']])
        self.assertNotIn(self.beta['entityId'], report['scope']['entities'])
        self.assertNotIn('ENTITY_AMBIGUOUS', [row['code'] for row in report['missing']])

    def test_task_scope_bounds_entity_and_arc_candidates_together(self):
        arc = self.source['arcs'][0]
        self.source['arcs'].extend({**arc, 'arcId': f'arc-TWIN-{i}', 'title': '共同弧线'} for i in range(400))
        self.alpha['name'] = '共同弧线'
        self.save()
        with self.assertRaises(StructureError) as caught:
            prepare_task(self.root, 'titler', chapter_id=self.cid, entities=['共同弧线'])
        self.assertEqual(caught.exception.code, 'QUERY_BUDGET_EXCEEDED')

    def test_task_executor_preserves_pov_blockers_instead_of_rereading_author_context(self):
        from scripts.core.tool_plan import execute_task_actions
        from tests.test_context_builder import context_source, save_context_source
        source = context_source(self.root)
        for entity in source['entities'][:2]:
            entity['name'] = '林舟'
        fragment = next(row for row in source['fragments'] if row['fragmentId'] == 'plan-PLAN')
        for selector, code in (('林舟', 'CHAPTER_CONTRACT_POV_AMBIGUOUS'),
                               ('character-MISSING', 'CHAPTER_CONTRACT_POV_UNKNOWN')):
            with self.subTest(selector=selector):
                fragment['chapterContract'] = {'targetRange': [1, 10000], 'pov': selector}
                save_context_source(self.root, source)
                result = execute_task_actions(self.root, 'planner', chapter_id='chapter-AAA')
                prepared = next(row['result'] for row in result['executed'] if row['operation'] == 'task.prepare')
                self.assertIn(code, [issue['code'] for issue in prepared['missing']])
                self.assertIsNone(prepared['modelContext'])
                self.assertNotIn('context.read', [row['operation'] for row in result['executed']])
                self.assertFalse(result['taskReady'])

    def test_large_candidates_use_existing_budget_without_issuing_a_binding(self):
        from scripts.core.project import query_policy
        self.twins()
        self.alpha['summary'] = '东门守卫的身份说明。' * 1000
        self.save()
        result = resolve_canon_target(self.root, selector='林舟')
        self.assertTrue(result['matches'][0]['summaryTruncated'])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), query_policy()['maxBytes']['detail'])
        self.source['entities'].extend({'entityId': f'character-TWIN-{i}', 'entityType': 'character', 'name': '林舟'}
                                       for i in range(400))
        self.save()
        with self.assertRaises(StructureError) as caught:
            resolve_canon_target(self.root, selector='林舟')
        self.assertEqual(caught.exception.code, 'QUERY_BUDGET_EXCEEDED')
        self.assertNotIn('sessionRef', caught.exception.details)

    def test_cli_and_mcp_agree_on_id_priority_and_ambiguity(self):
        from scripts.mcp_server import handle
        self.twins()
        self.beta['aliases'].append(self.alpha['entityId'])
        self.save()
        repo = Path(__file__).resolve().parents[1]
        for selector in ('林舟', self.alpha['entityId']):
            cli = subprocess.run([sys.executable, str(repo / 'scripts/novelctl.py'), 'project', 'source',
                'canon-resolve', '--project-root', str(self.root), '--selector', selector],
                cwd=repo, capture_output=True, text=True, timeout=60)
            self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
            mcp = handle({'id': 1, 'method': 'tools/call', 'params': {'name': 'resolve_source_canon_target',
                'arguments': {'projectRoot': str(self.root), 'selector': selector}}})['result']
            self.assertFalse(mcp['isError'], mcp)
            self.assertEqual(json.loads(cli.stdout), mcp['structuredContent'])

    def test_cognition_scanner_retains_same_name_candidates_without_assigning_speaker(self):
        from scripts.core.cognition_review import scan_candidates
        self.twins()
        rows = scan_candidates(self.source, self.cid, '林舟说：“林舟带着钥匙。”')
        self.assertEqual(rows[0]['mentions'][0]['entityIds'], [self.alpha['entityId'], self.beta['entityId']])
        self.assertIsNone(rows[0]['speakerHint']['characterId'])
        self.assertFalse(rows[0]['speakerHint']['certain'])


if __name__ == '__main__':
    unittest.main()
