"""Author-facing review views and notes are bound to actual registered evidence."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import file_sha256, sha256_json, sha256_text
from scripts.core.candidate_versions import activate_candidate_version, save_candidate_version
from scripts.core.finalization import finalize_chapter
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.review_localization import load_issue_notes, note_issue, review_report_view
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from tests.fixtures_workflow import assessed_review_payload, evidenced_slots, review_ready_project


BODY = '他把钥匙放回桌上，合上了登记本。'


class ReviewReportIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='review-report-integrity-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        fixture = review_ready_project(self.root, chapter_count=2)
        self.chapter = fixture['chapterIds'][0]
        self.other = fixture['chapterIds'][1]
        self.report = self.register_report()

    def register_report(self, *, label='原建议'):
        source = load_structure(self.root)
        findings = [
            {'severity': 'S3', 'ruleId': 'R-RHYTHM', 'summary': label + '：句子可考虑拆开',
             'location': '他把钥匙放回桌上', 'repairSuggestion': '可保留原句，也可拆开动作。', 'advisory': True},
            {'severity': 'S3', 'ruleId': 'R-WORD', 'summary': '重复词的备选表达',
             'location': '上', 'repairSuggestion': '作者可按节奏选择。', 'advisory': True},
        ]
        payload = assessed_review_payload(source, self.chapter, 'stage_b_review', {
            'verdict': 'REPAIR_REQUIRED', 'repairScope': 'local',
            'slots': evidenced_slots(BODY), 'findings': findings})
        result = pack_artifact(self.root, chapterId=self.chapter, artifactType='stage_b_review', payload=payload)
        return read_json(self.root / result['artifactPath'])

    def entry(self, kind='stage_b_review'):
        return read_json(self.root / '.govel/workflow.json')['artifacts'][self.chapter][kind]

    def artifact_path(self, kind='stage_b_review'):
        value = Path(self.entry(kind)['artifactPath'])
        return value if value.is_absolute() else self.root / value

    def view(self, chapter=None):
        return call_operation('review.report-view', self.root, {'chapterId': chapter or self.chapter})

    def note(self, **overrides):
        args = {'chapterId': self.chapter, 'reportSha256': self.report['artifactSha256'],
                'findingIndex': 0, 'state': 'processed', 'note': '保留原句，已检查这条建议。'}
        return call_operation('review.note-issue', self.root, {**args, **overrides})

    def inventory(self):
        result = {}
        for path in self.root.rglob('*'):
            if path.is_symlink():
                result[str(path.relative_to(self.root))] = ('symlink', os.readlink(path))
            elif path.is_file():
                result[str(path.relative_to(self.root))] = ('file', path.read_bytes())
        return result

    def rehash_report(self, artifact, *, update_registration=False, path=None):
        path = path or self.artifact_path()
        artifact['artifactSha256'] = sha256_json({key: value for key, value in artifact.items() if key != 'artifactSha256'})
        write_json(path, artifact)
        if update_registration:
            workflow = read_json(self.root / '.govel/workflow.json')
            entry = workflow['artifacts'][self.chapter]['stage_b_review']
            entry.update(artifactSha256=artifact['artifactSha256'], artifactFileSha256=file_sha256(path), artifactPath=str(path))
            write_json(self.root / '.govel/workflow.json', workflow)

    def assert_no_current_offsets(self, result):
        self.assertFalse(result['versionBinding']['current'])
        self.assertFalse(result['canAnnotate'])
        self.assertTrue(all(not row['anchor']['offsets'] for row in result['localization'] or []))

    def assert_unreadable(self):
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.view()
        self.assertEqual(caught.exception.code, 'REVIEW_REPORT_UNREADABLE')
        self.assertEqual(self.inventory(), before)

    def activate(self, body):
        saved = save_candidate_version(self.root, self.chapter, body=body, request_id='active-1',
                                       base_candidate_version=None)
        activate_candidate_version(self.root, self.chapter, candidate_version=saved['version'], expected_active_token=0)
        return self.root / '.govel/workbench/candidates' / self.chapter / saved['fileName']

    def change_plan_without_changing_report(self):
        source = load_structure(self.root)
        plan = next(row for row in source['fragments'] if row['ownerId'] == self.chapter and row['contentRole'] == 'plan')
        plan['text'] += '\n另一轮已改过这份计划。'
        validate_structure(source)
        # Deliberate source drift without invalidation metadata proves the reader
        # verifies actual bindings instead of trusting absence of an invalid flag.
        write_json(self.root / '.govel/structure.json', source)

    def test_public_view_reads_verified_candidate_and_exact_current_locations(self):
        before = self.inventory()
        result = self.view()
        self.assertEqual(result['report']['artifactSha256'], self.report['artifactSha256'])
        self.assertEqual(result['report']['findings'], self.report['payload']['findings'])
        self.assertEqual(result['versionBinding']['state'], 'current')
        self.assertTrue(result['versionBinding']['known'])
        self.assertTrue(result['versionBinding']['matchesBody'])
        self.assertTrue(result['versionBinding']['current'])
        self.assertTrue(result['canAnnotate'])
        self.assertEqual(result['localizedAgainst']['kind'], 'candidate_prose')
        self.assertEqual(result['localizedAgainst']['bodySha256'], sha256_text(BODY))
        self.assertEqual(result['localization'][0]['anchor']['offsets'], [[0, len('他把钥匙放回桌上')]])
        self.assertEqual(result['localization'][1]['anchor']['state'], 'ambiguous')
        self.assertEqual(self.inventory(), before)

    def test_direct_body_and_source_label_cannot_fabricate_a_current_binding(self):
        for body, kind in ((BODY + '后来又改了一句。', 'candidate_prose'), (BODY, 'active_candidate')):
            with self.subTest(body=body, kind=kind):
                result = review_report_view(self.root, self.chapter, body=body, body_kind=kind)
                self.assert_no_current_offsets(result)

    def test_absent_report_and_unavailable_report_are_distinct(self):
        absent = self.view(self.other)
        self.assertEqual(absent['code'], 'REVIEW_REPORT_ABSENT')
        self.assertIsNone(absent['report'])
        self.assertEqual(absent['versionBinding']['state'], 'absent')
        self.artifact_path().unlink()
        self.assert_unreadable()

    def test_payload_edit_without_rehash_is_not_shown_as_a_registered_report(self):
        artifact = read_json(self.artifact_path())
        artifact['payload']['findings'][0]['summary'] = '外部改写内容'
        write_json(self.artifact_path(), artifact)
        self.assert_unreadable()

    def test_rehashed_payload_still_requires_the_registered_artifact_hash(self):
        artifact = read_json(self.artifact_path())
        artifact['payload']['findings'][0]['summary'] = '已重算信封但未登记'
        self.rehash_report(artifact)
        self.assert_unreadable()

    def test_file_only_whitespace_change_invalidates_the_registered_file_digest(self):
        path = self.artifact_path()
        path.write_bytes(path.read_bytes() + b'\n')
        self.assert_unreadable()

    def test_missing_reviewed_body_hash_is_not_a_current_format_review(self):
        artifact = deepcopy(self.report)
        artifact['payload'].pop('finalProseSha256')
        self.rehash_report(artifact, update_registration=True)
        self.assert_unreadable()

    def test_matching_registry_hashes_do_not_excuse_wrong_project_chapter_or_kind(self):
        original = deepcopy(self.report)
        for key, value in (('projectId', 'different-project'), ('chapterId', self.other), ('artifactType', 'stage_a_review')):
            with self.subTest(field=key):
                artifact = deepcopy(original)
                artifact[key] = value
                self.rehash_report(artifact, update_registration=True)
                self.assert_unreadable()

    def test_report_path_must_stay_inside_project_even_with_matching_hashes(self):
        external = self.root.parent / (self.root.name + '-outside.json')
        self.addCleanup(external.unlink, missing_ok=True)
        self.rehash_report(deepcopy(self.report), update_registration=True, path=external)
        self.assert_unreadable()

    def test_report_symlink_is_rejected_instead_of_followed(self):
        path = self.artifact_path()
        preserved = path.with_name('preserved-report.json')
        path.rename(preserved)
        path.symlink_to(preserved.name)
        self.assert_unreadable()

    def test_corrupt_current_report_never_falls_back_to_a_readable_history(self):
        previous = self.artifact_path()
        self.report = self.register_report(label='新版建议')
        self.assertTrue(previous.is_file())
        self.artifact_path().write_bytes(b'{')
        self.assert_unreadable()

    def test_corrupt_or_missing_registered_candidate_does_not_fall_back_to_formal(self):
        path = self.artifact_path('candidate_prose')
        original = path.read_bytes()
        for contents in (b'{', None):
            with self.subTest(contents=contents):
                if contents is None:
                    path.unlink()
                else:
                    path.write_bytes(contents)
                result = self.view()
                self.assertEqual(result['report']['artifactSha256'], self.report['artifactSha256'])
                self.assertEqual(result['versionBinding']['state'], 'unavailable')
                self.assert_no_current_offsets(result)
                path.write_bytes(original)

    def test_invalidated_report_keeps_advice_and_notes_without_current_offsets_or_approval(self):
        self.note()
        report_bytes = self.artifact_path().read_bytes()
        self.activate(BODY + '下一轮候选增补了说明。')
        workflow_bytes = (self.root / '.govel/workflow.json').read_bytes()
        before = self.inventory()
        result = self.view()
        self.assertEqual(result['code'], 'REVIEW_REPORT')
        self.assertEqual(result['versionBinding']['state'], 'historical')
        self.assertFalse(result['versionBinding']['matchesBody'])
        self.assert_no_current_offsets(result)
        self.assertEqual(result['report']['findings'], self.report['payload']['findings'])
        self.assertEqual(result['notes'][f"{self.report['artifactSha256']}:0"]['state'], 'processed')
        self.assertTrue(all(row['anchor']['state'] == 'historical' for row in result['localization']))
        self.assertEqual(self.artifact_path().read_bytes(), report_bytes)
        self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), workflow_bytes)
        self.assertEqual(self.inventory(), before)
        with self.assertRaises(StructureError):
            self.note()
        self.assertEqual(self.inventory(), before)

    def test_active_workbench_same_text_does_not_revive_invalidated_report(self):
        self.activate(BODY)
        result = self.view()
        self.assertTrue(result['versionBinding']['matchesBody'])
        self.assertEqual(result['versionBinding']['state'], 'historical')
        self.assert_no_current_offsets(result)

    def test_corrupt_active_candidate_is_unavailable_even_when_registered_body_is_intact(self):
        path = self.activate(BODY)
        path.write_text('未登记的外部改动。')
        result = self.view()
        self.assertEqual(result['versionBinding']['state'], 'unavailable')
        self.assert_no_current_offsets(result)
        self.assertIsNone(result['localizedAgainst']['bodySha256'])

    def test_missing_active_candidate_is_unavailable_without_silent_fallback(self):
        path = self.activate(BODY)
        path.unlink()
        result = self.view()
        self.assertEqual(result['versionBinding']['state'], 'unavailable')
        self.assert_no_current_offsets(result)

    def test_unparseable_active_index_is_unavailable_without_silent_fallback(self):
        self.activate(BODY)
        (self.root / '.govel/workbench/candidates' / self.chapter / 'index.json').write_text('{')
        result = self.view()
        self.assertEqual(result['versionBinding']['state'], 'unavailable')
        self.assert_no_current_offsets(result)

    def test_source_plan_drift_is_not_current_even_when_report_and_body_match(self):
        self.change_plan_without_changing_report()
        before = self.inventory()
        result = self.view()
        self.assertTrue(result['versionBinding']['known'])
        self.assertTrue(result['versionBinding']['matchesBody'])
        self.assert_no_current_offsets(result)
        self.assertTrue(result['versionBinding']['reasons'])
        self.assertEqual(self.inventory(), before)
        with self.assertRaises(StructureError):
            self.note()
        self.assertEqual(self.inventory(), before)

    def test_stage_a_artifact_drift_is_not_current_even_with_matching_prose(self):
        path = self.artifact_path('stage_a_review')
        path.write_bytes(path.read_bytes() + b'\n')
        result = self.view()
        self.assert_no_current_offsets(result)
        self.assertEqual(result['versionBinding']['state'], 'unavailable')
        self.assertTrue(result['versionBinding']['reasons'])

    def test_missing_and_corrupt_proof_inputs_are_unavailable_not_merely_historical(self):
        workflow_path = self.root / '.govel/workflow.json'
        original_workflow = workflow_path.read_bytes()
        for kind in ('stage_a_review', 'diagnostics'):
            path = self.artifact_path(kind)
            original = path.read_bytes()
            for mutation in ('file_missing', 'file_changed', 'entry_missing'):
                with self.subTest(kind=kind, mutation=mutation):
                    if mutation == 'file_missing':
                        path.unlink()
                    elif mutation == 'file_changed':
                        path.write_bytes(original + b'\n')
                    else:
                        workflow = read_json(workflow_path)
                        workflow['artifacts'][self.chapter].pop(kind)
                        write_json(workflow_path, workflow)
                    view = self.view()
                    self.assertEqual(view['versionBinding']['state'], 'unavailable')
                    self.assert_no_current_offsets(view)
                    path.write_bytes(original)
                    workflow_path.write_bytes(original_workflow)

    def test_invalid_activation_state_cannot_fall_back_to_registered_candidate(self):
        from scripts.core.candidate_versions import list_candidate_versions
        self.activate('另一份与登记稿不同的工作台候选。')
        with self.assertRaises(StructureError) as caught:
            self.register_report(label='登记候选的报告')
        self.assertEqual(caught.exception.code, 'CURRENT_CANDIDATE_UNALIGNED')
        path = self.root / '.govel/workbench/candidates' / self.chapter / 'index.json'
        original = read_json(path)
        for missing in (True, False):
            index = deepcopy(original)
            if missing:
                index.pop('activeVersion')
            else:
                index['activeVersion'] = None
            write_json(path, index)
            with self.subTest(missing=missing):
                view = self.view()
                self.assertEqual(view['versionBinding']['state'], 'unavailable')
                self.assert_no_current_offsets(view)
                with self.assertRaises(StructureError):
                    list_candidate_versions(self.root, self.chapter)
                with self.assertRaises(StructureError):
                    self.note()

    def test_stale_plan_or_stage_a_cannot_hide_unavailable_diagnostics(self):
        source_path = self.root / '.govel/structure.json'
        workflow_path = self.root / '.govel/workflow.json'
        original_source, original_workflow = source_path.read_bytes(), workflow_path.read_bytes()
        diagnostics_path = self.artifact_path('diagnostics')
        original_diagnostics = diagnostics_path.read_bytes()
        for stale in ('plan', 'stage_a'):
            for broken in ('missing', 'changed', 'entry_missing'):
                with self.subTest(stale=stale, broken=broken):
                    source_path.write_bytes(original_source)
                    workflow_path.write_bytes(original_workflow)
                    diagnostics_path.write_bytes(original_diagnostics)
                    if stale == 'plan':
                        self.change_plan_without_changing_report()
                    else:
                        workflow = read_json(workflow_path)
                        workflow['artifacts'][self.chapter]['stage_a_review']['invalidatedBy'] = {'reason': 'test source change'}
                        write_json(workflow_path, workflow)
                    if broken == 'missing':
                        diagnostics_path.unlink()
                    elif broken == 'changed':
                        diagnostics_path.write_bytes(original_diagnostics + b'\n')
                    else:
                        workflow = read_json(workflow_path)
                        workflow['artifacts'][self.chapter].pop('diagnostics')
                        write_json(workflow_path, workflow)
                    view = self.view()
                    self.assertEqual(view['versionBinding']['state'], 'unavailable')
                    self.assert_no_current_offsets(view)
                    self.assertTrue(any(row.get('unavailable') for row in view['versionBinding']['reasons']))

    def test_formal_alias_created_during_note_transaction_is_guarded(self):
        from scripts.core.workflow import approve_artifacts
        # Publish a real reviewed fixture, then choose the verified formal source.
        result = pack_artifact(self.root, chapterId=self.chapter, artifactType='stage_b_review', payload={
            'verdict': 'POLISH_READY', 'slots': evidenced_slots(BODY),
            'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'},
            'factAssessment': {'status': 'no_change'}, 'findings': self.report['payload']['findings']})
        workflow = read_json(self.root / '.govel/workflow.json')
        approve_artifacts(workflow, self.chapter, artifact_hashes={'stage_b_review': result['artifactSha256']}, approval_mode='manual', project_root=self.root)
        write_json(self.root / '.govel/workflow.json', workflow)
        source = load_structure(self.root)
        finalize_chapter(self.root, self.chapter, skip_title=True, binding_generation=source['chapters'][0]['proseBinding']['generation'])
        workflow = read_json(self.root / '.govel/workflow.json')
        workflow['artifacts'][self.chapter].pop('candidate_prose')
        write_json(self.root / '.govel/workflow.json', workflow)
        self.assertTrue(self.view()['canAnnotate'])
        source = load_structure(self.root)
        first, second = [self.root / row['filePath'] for row in source['chapters']]

        def conflict(phase, relative=None):
            if phase == 'prepared':
                second.unlink(missing_ok=True)
                os.link(first, second)

        with patch('scripts.core.write_sets.checkpoint', side_effect=conflict), self.assertRaises(ValueError):
            self.note(reportSha256=result['artifactSha256'])
        self.assertTrue(first.samefile(second))
        self.assertEqual(load_issue_notes(self.root, self.chapter)['notes'], {})

    def test_same_body_new_report_does_not_receive_old_notes(self):
        self.note()
        old_key = f"{self.report['artifactSha256']}:0"
        self.report = self.register_report(label='新版建议')
        result = self.view()
        self.assertTrue(result['versionBinding']['current'])
        self.assertNotIn(f"{self.report['artifactSha256']}:0", result['notes'])
        self.assertEqual(load_issue_notes(self.root, self.chapter)['notes'][old_key]['state'], 'processed')

    def test_note_is_work_state_only_and_does_not_change_report_source_or_workflow(self):
        protected = {path: path.read_bytes() for path in (
            self.root / '.govel/structure.json', self.root / '.govel/workflow.json',
            self.artifact_path(), self.artifact_path('candidate_prose'))}
        result = self.note()
        self.assertEqual(result['code'], 'REVIEW_NOTED')
        self.assertEqual(result['state'], 'processed')
        for path, raw in protected.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.view()['report']['verdict'], 'REPAIR_REQUIRED')

    def test_report_change_between_body_reads_does_not_return_a_mixed_snapshot(self):
        from scripts.core import review_report_sources
        observe = review_report_sources.guard_observation
        report_path = self.artifact_path()
        candidate_path = self.artifact_path('candidate_prose')
        original_report = report_path.read_bytes()
        original_workflow = (self.root / '.govel/workflow.json').read_bytes()

        def racing_observation(path):
            result = observe(path)
            if path == candidate_path:
                report_path.write_bytes(original_report + b'\n')
            return result

        with patch('scripts.core.review_report_sources.guard_observation', side_effect=racing_observation), self.assertRaises(ValueError):
            self.view()
        self.assertEqual(report_path.read_bytes(), original_report + b'\n')
        self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), original_workflow)

    def test_ghost_boolean_negative_and_out_of_range_indices_never_create_notes(self):
        for index in (True, False, -1, 2, 100000, 0.0, '0'):
            before = self.inventory()
            with self.subTest(index=index), self.assertRaises(ValueError):
                note_issue(self.root, self.chapter, report_sha256=self.report['artifactSha256'],
                           finding_index=index, state='processed')
            self.assertEqual(self.inventory(), before)
        before = self.inventory()
        with self.assertRaises(ValueError):
            self.note(chapterId=self.other)
        self.assertEqual(self.inventory(), before)

    def test_wrong_report_hash_is_rejected_without_note_writes(self):
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.note(reportSha256='0' * 64)
        self.assertEqual(caught.exception.code, 'REVIEW_REPORT_STALE')
        self.assertEqual(self.inventory(), before)

    def test_corrupt_report_cannot_be_annotated_even_with_its_registered_hash(self):
        self.artifact_path().write_bytes(b'{')
        before = self.inventory()
        with self.assertRaises(ValueError):
            self.note()
        self.assertEqual(self.inventory(), before)

    def test_changed_report_during_prepared_note_transaction_is_not_overwritten_or_annotated(self):
        path = self.artifact_path()
        original = path.read_bytes()
        note_path = self.root / '.govel/workbench/review-notes' / f'{self.chapter}.json'
        workflow = (self.root / '.govel/workflow.json').read_bytes()

        def change(phase, relative=None):
            if phase == 'prepared':
                path.write_bytes(original + b'\n')

        with patch('scripts.core.write_sets.checkpoint', side_effect=change), self.assertRaises(ValueError):
            self.note()
        self.assertEqual(path.read_bytes(), original + b'\n')
        self.assertFalse(note_path.exists())
        self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), workflow)

    def test_candidate_change_after_note_write_rolls_back_only_the_note(self):
        self.note(note='已有笔记')
        notes_path = self.root / '.govel/workbench/review-notes' / f'{self.chapter}.json'
        original_notes = notes_path.read_bytes()
        candidate_path = self.artifact_path('candidate_prose')
        original_candidate = candidate_path.read_bytes()

        def change(phase, relative=None):
            if phase == 'write' and relative == str(notes_path.relative_to(self.root)):
                candidate_path.write_bytes(original_candidate + b'\n')

        with patch('scripts.core.write_sets.checkpoint', side_effect=change), self.assertRaises(ValueError):
            self.note(note='这条不得提交')
        self.assertEqual(notes_path.read_bytes(), original_notes)
        self.assertEqual(candidate_path.read_bytes(), original_candidate + b'\n')

    def test_active_index_change_during_note_commit_rejects_the_old_snapshot(self):
        self.activate(BODY)
        from scripts.core.current_candidate import prepare_candidate_handoff
        frozen = prepare_candidate_handoff(self.root, chapterId=self.chapter)
        pack_artifact(self.root, chapterId=self.chapter, artifactType='candidate_prose', text=BODY,
            selectionRef=frozen['selectionRef'], selectionReview={
                'planApplicabilityReason': '合成夹具实际保留同一动作展开，核对原计划及阶段A适用。'})
        self.report = self.register_report(label='激活稿已检查')
        self.assertTrue(self.view()['canAnnotate'])
        index_path = self.root / '.govel/workbench/candidates' / self.chapter / 'index.json'
        index = read_json(index_path)
        changed = {**index, 'activeToken': index['activeToken'] + 1}
        notes_path = self.root / '.govel/workbench/review-notes' / f'{self.chapter}.json'

        def change(phase, relative=None):
            if phase == 'prepared':
                write_json(index_path, changed)

        with patch('scripts.core.write_sets.checkpoint', side_effect=change), self.assertRaises(ValueError):
            self.note()
        self.assertFalse(notes_path.exists())
        self.assertEqual(read_json(index_path), changed)

    def test_competing_note_edit_is_preserved_when_our_write_is_rejected(self):
        self.note(note='原笔记')
        notes_path = self.root / '.govel/workbench/review-notes' / f'{self.chapter}.json'
        changed = read_json(notes_path)
        key = f"{self.report['artifactSha256']}:0"
        changed['notes'][key]['note'] = '另一个编辑器的新笔记'

        def change(phase, relative=None):
            if phase == 'prepared':
                write_json(notes_path, changed)

        with patch('scripts.core.write_sets.checkpoint', side_effect=change), self.assertRaises((ValueError, OSError)):
            self.note(note='不应覆盖并发笔记')
        self.assertEqual(read_json(notes_path), changed)

    def test_formal_fallback_verifies_clean_ownership_and_rejects_external_change(self):
        # A separate real finalized fixture avoids fabricating managed ownership.
        with tempfile.TemporaryDirectory(prefix='review-formal-') as temp:
            root = Path(temp).resolve()
            fixture = review_ready_project(root, chapter_count=2)
            cid = fixture['chapterIds'][0]
            source = load_structure(root)
            generation = source['chapters'][0]['proseBinding']['generation']
            finalize_chapter(root, cid, skip_title=True, binding_generation=generation)
            workflow = read_json(root / '.govel/workflow.json')
            workflow['artifacts'][cid].pop('candidate_prose')
            write_json(root / '.govel/workflow.json', workflow)
            current = call_operation('review.report-view', root, {'chapterId': cid})
            self.assertEqual(current['localizedAgainst']['kind'], 'formal_prose')
            self.assertTrue(current['versionBinding']['matchesBody'])
            self.assertTrue(current['versionBinding']['current'])
            self.assertTrue(current['canAnnotate'])
            source = load_structure(root)
            sibling = root / source['chapters'][1]['filePath']
            sibling.unlink(missing_ok=True)
            sibling.symlink_to('unavailable-sibling-prose.md')
            # Reading this clean report must not inherit a broken sibling's
            # path. Only formal note mutations take conservative topology guards.
            self.assertTrue(call_operation('review.report-view', root, {'chapterId': cid})['versionBinding']['current'])
            sibling.unlink()
            formal = root / source['chapters'][0]['filePath']
            formal.write_text(BODY + '作者尚未接纳的外部编辑。')
            changed = call_operation('review.report-view', root, {'chapterId': cid})
            self.assertEqual(changed['versionBinding']['state'], 'unavailable')
            self.assert_no_current_offsets(changed)


if __name__ == '__main__':
    unittest.main()
