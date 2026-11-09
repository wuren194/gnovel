"""Transport optimization must preserve scope, exact content and retry boundaries."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError
from scripts.core.jsonio import read_json, write_json
from tests.fixtures_workflow import review_ready_project, register_and_approve


class TransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def chapter(self):
        return review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def test_static_cache_refreshes_restored_mtime_and_returns_independent_values(self):
        from scripts.core.static_cache import cached_json, cached_sha256
        path = self.root / 'registry.json'; path.write_text('{"value":1}')
        old = path.stat(); first = cached_sha256(path)
        value = cached_json(path); value['value'] = 100
        self.assertEqual(cached_json(path), {'value': 1})
        path.write_text('{"value":2}'); os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(cached_json(path), {'value': 2})
        self.assertNotEqual(first, cached_sha256(path))

    def test_cached_derived_reruns_only_on_byte_change_and_never_caches_failure(self):
        from scripts.core.static_cache import cached_derived
        path = self.root / 'policy.json'; path.write_text('{"value":1}')
        calls = []

        def derive(value):
            calls.append(value)
            if value['value'] < 0:
                raise ValueError('invalid policy')
            return {'value': value['value'], 'items': [value['value']]}

        first = cached_derived(path, derive); first['items'].append(99)
        self.assertEqual(cached_derived(path, derive), {'value': 1, 'items': [1]})
        self.assertEqual(len(calls), 1)
        old = path.stat()
        path.write_text('{"value":2}'); os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(cached_derived(path, derive)['value'], 2)
        path.write_text('{"value":-1}')
        for _ in range(2):
            with self.assertRaises(ValueError):
                cached_derived(path, derive)
        self.assertEqual(len(calls), 4)

    def test_source_model_policy_validates_once_per_registry_change(self):
        """结构校验每条记录都读一次这份策略；每次都重解析重校验曾占加载耗时的八成以上。"""
        from scripts.core import project
        with patch.object(project, '_validated_source_model', wraps=project._validated_source_model) as validated:
            policy = project.source_model_policy(); policy['placementScopes'].append('unsupported')
            for _ in range(50):
                self.assertNotIn('unsupported', project.source_model_policy()['placementScopes'])
        self.assertEqual(validated.call_count, 1)
        relative = project.toolbox_manifest()['sharedProtocols']['sourceModel']
        registry = self.root / 'toolbox' / relative
        registry.parent.mkdir(parents=True)
        original = read_json(project.TOOLBOX_ROOT / relative)
        write_json(registry, original)
        with patch.object(project, 'TOOLBOX_ROOT', self.root / 'toolbox'):
            self.assertEqual(project.source_model_policy(), original)
            # 运行中改坏注册表：下一次读取即 fail closed，不沿用已缓存的旧策略。
            write_json(registry, {**original, 'placementScopes': ['volume']})
            with self.assertRaises(ValueError):
                project.source_model_policy()

    def test_derived_index_version_ignores_only_metadata_not_bytes_or_identity(self):
        from scripts.core.static_cache import derived_index_signature
        path = self.root / 'derived.sqlite3'
        path.write_bytes(b'first')
        initial = path.stat()
        before = derived_index_signature(path)
        os.utime(path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        self.assertEqual(derived_index_signature(path), before)
        path.write_bytes(b'other')
        os.utime(path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        after = derived_index_signature(path)
        self.assertNotEqual(after, before)
        replacement = self.root / 'replacement.sqlite3'
        replacement.write_bytes(b'other')
        os.replace(replacement, path)
        self.assertNotEqual(derived_index_signature(path), after)

    def test_missing_cross_project_and_changed_preparation_fail_closed(self):
        from scripts.core.transport_cache import get, _directory
        cid = self.chapter()
        report = call_operation('task.prepare', self.root, {'task': 'editor', 'chapterId': cid, 'view': 'model'})
        reference = report['reading']['credential']
        with self.assertRaises(StructureError): get(self.root / 'other', reference, kind='prepare')
        arguments = {'credential': reference, 'readIds': report['contractReads'][0]['readIds']}
        marker = self.root / 'AGENTS.md'; marker.write_text('新约束')
        with self.assertRaises(StructureError): call_operation('task.read-batch', self.root, arguments)
        marker.unlink()
        (_directory() / (reference.replace(':', '-') + '.json')).unlink()
        with self.assertRaises(StructureError): call_operation('task.read-batch', self.root, arguments)

    def test_source_batch_pages_long_visible_text_and_never_delivers_other_pov(self):
        from tests.context_semantic_fixture import fixture
        from tests.test_context_builder import save_context_source
        cid, goal = fixture(self.root, 'small')
        source = read_json(self.root / '.gnovel/structure.json')
        source['pairAssessment'] = {'status': 'not_applicable', 'reason': '本合成视角传输测试没有配对情节。'}
        visible = next(r for r in source['fragments'] if r['fragmentId'] == 'SF-BELIEF')
        visible['text'] = '甲认得门上的字。' * 1800
        hidden = next(r for r in source['fragments'] if r['fragmentId'] == 'SF-TEXT')
        hidden['text'] = '乙的隐藏线索不能泄漏'
        save_context_source(self.root, source)
        report = call_operation('task.prepare', self.root, {'task': 'planner', 'chapterId': cid,
            'povCharacter': 'C-ALPHA', 'chapterGoal': goal, 'view': 'model'})
        ids = [r['readId'] for r in report['reading']['sourceReads']]
        self.assertTrue(ids)
        args = {'credential': report['reading']['credential'], 'readIds': ids, 'maxBytes': 6000}
        pages = []; chunks = {}; cursor = None
        while True:
            result = call_operation('task.read-batch', self.root, {**args, **({'cursor': cursor} if cursor else {})})
            self.assertTrue(result['coverage']['sourceQueriesExecuted'])
            pages.append(result)
            for row in result['items']: chunks.setdefault(row['id'], []).append(row['text'])
            if not result['hasMore']: break
            cursor = result['nextCursor']
        self.assertGreater(len(pages), 1)
        records = [json.loads(''.join(parts)) for parts in chunks.values()]
        record = next(r for r in records if r['id'] == 'SF-BELIEF')
        self.assertEqual(record['content']['text'], visible['text'])
        self.assertNotIn(hidden['text'], json.dumps(pages, ensure_ascii=False))
        self.assertNotIn('SF-TEXT', {r['id'] for r in records})

    def test_long_repair_windows_preserve_all_text_and_distinct_rules(self):
        from scripts.core.artifacts import sha256_text
        from scripts.core.review_artifacts import STAGE_B_SLOTS, AUXILIARY_GATES
        cid = self.chapter(); body = '甲' * 3000 + '唯一定位句。' + '乙' * 3000
        # This fixture measures two explicit review issues, not old mechanical
        # findings attached to the original short body.
        workflow = read_json(self.root/'.gnovel/workflow.json')
        workflow['artifacts'][cid].pop('diagnostics', None)
        workflow['approvals'][cid].get('artifactHashes', {}).pop('diagnostics', None)
        write_json(self.root/'.gnovel/workflow.json', workflow)
        (self.root / '工作台/input.txt').write_text(body)
        call_operation('workflow.artifact-pack', self.root, {'chapterId': cid, 'artifactType': 'candidate_prose', 'input': '工作台/input.txt'})
        findings = [{'severity': 'S2', 'ruleId': rule, 'summary': '独立问题', 'location': body,
                     'repairSuggestion': '由工位判断'} for rule in ('one', 'two')]
        register_and_approve(self.root, cid, 'stage_b_review', {'verdict': 'REPAIR_REQUIRED', 'repairScope': 'local',
            'slots': {s: {'conclusion': '测试'} for s in (*STAGE_B_SLOTS, *AUXILIARY_GATES)},
            'findings': findings, 'finalProseSha256': sha256_text(body)})
        cursor = None; windows = {}; issues = {}; pages = 0
        while True:
            page = call_operation('workflow.repair-context', self.root, {'chapterId': cid, 'maxBytes': 6000,
                **({'cursor': cursor} if cursor else {})}); pages += 1
            for row in page['items']:
                if row['source'] == 'issuePreview':
                    self.assertEqual(row['finding']['summary'], '独立问题')
                    continue
                if row['source'] == 'unavailableReport':
                    self.assertGreater(page['coverage']['unavailableReportCount'], 0)
                    continue
                target = windows if row['source'] == 'contextWindow' else issues
                target.setdefault(row['id'], []).append(row['text'])
            if not page['hasMore']: break
            cursor = page['nextCursor']
        self.assertGreater(pages, 1); self.assertEqual(len(windows), 1)
        self.assertEqual(''.join(next(iter(windows.values()))), body)
        restored = [json.loads(''.join(parts))['finding'] for parts in issues.values()]
        self.assertEqual({r['ruleId'] for r in restored}, {'one', 'two'})
        self.assertTrue(all(r['location'] == body for r in restored))

    def test_patch_retry_resumes_diagnostics_and_rejects_different_inputs(self):
        cid = self.chapter()
        report = call_operation('task.prepare', self.root, {'task': 'editor', 'chapterId': cid, 'view': 'model'})
        args = {'credential': report['reading']['credential'], 'requestId': 'retry-patch',
                'advanceLifecycle': True, 'diagnostics': {'mode': 'plot', 'intensityLevel': 1},
                'patches': [{'issueId': 'one', 'original': {'text': '合上了登记本'}, 'replacement': '翻开了登记本'}]}
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=OSError('injected')):
            first = call_operation('workflow.patch-candidate', self.root, args)
        self.assertFalse(first['ok']); self.assertTrue(first['registered'])
        retry = call_operation('workflow.patch-candidate', self.root, args)
        self.assertTrue(retry['ok'], retry); self.assertTrue(retry['replayed'])
        self.assertEqual(first['artifactSha256'], retry['artifactSha256'])
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=AssertionError('must reuse')):
            again = call_operation('workflow.patch-candidate', self.root, args)
        self.assertTrue(again['diagnostics']['reused'])
        with self.assertRaises(StructureError):
            call_operation('workflow.patch-candidate', self.root, {**args, 'patches': [{**args['patches'][0], 'replacement': '别的改动'}]})

    def test_model_summary_reference_roundtrip_rejects_changed_prose(self):
        from tests.fixtures_current import create_schema5_project
        from scripts.story_summarizer import _commit_summary, StorySummarizerError
        create_schema5_project(self.root, chapter_count=2)
        prepared = call_operation('summary.prepare', self.root, {})
        self.assertNotIn('inputSnapshot', prepared)
        payload = {k: prepared[k] for k in ('projectId', 'asOfChapterId', 'structureRevision', 'coveredChapterIds', 'inputSnapshotRef')}
        payload.update(schemaVersion=prepared['schemaVersion'], storySummary='测试摘要', plotLine=[], characterSummaries=[])
        path = self.root / 'summary.json'; write_json(path, payload)
        self.assertTrue(_commit_summary(self.root, str(path))['ok'])
        saved = read_json(self.root / '.gnovel/aux/story-summary.json')
        self.assertIn('inputSnapshot', saved)
        prose = self.root / prepared['chapters'][0]['relativePath']; prose.write_text('准备后被修改')
        with self.assertRaises(StorySummarizerError): _commit_summary(self.root, str(path))
