"""Doctor paging preserves failed checks; comparison is not semantic clearance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.doctor_delivery import doctor_report
from scripts.core.jsonio import write_json
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError
from tests.fixtures_source import source_fixture


def issue(code='BLOCK', message='Needs an explicit review', severity='blocking'):
    return {'code': code, 'domain': 'canon', 'objectType': 'fragment', 'objectId': 'SF-LIFE',
            'severity': severity, 'status': 'semantic_review_required', 'message': message,
            'evidence': {'long': 'proof'}, 'recommendedAction': 'Review it', 'affectedOperations': ['source.review-submit']}


class DoctorDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.source = source_fixture(self.root)
        self.report = {'ok': True, 'code': 'DOCTOR_ERRORS', 'healthy': False, 'strict': True,
                       'structureRevision': 3, 'scopedChapterId': None, 'project': {'projectId': self.source['projectId']},
                       'issueCounts': {'total': 1, 'blocking': 1, 'warning': 0, 'informational': 0},
                       'domains': {'canon': {'status': 'semantic_review_required', 'issueCount': 1, 'details': 'large' * 10000}},
                       'issues': [issue()], 'suggestedActions': [{'operation': 'source.reviews'}]}
        self.mock = patch('scripts.core.project_doctor.assess_project', side_effect=lambda *a, **k: deepcopy(self.report))
        self.assess = self.mock.start(); self.addCleanup(self.mock.stop)

    def prepare(self, **kwargs):
        return doctor_report(self.root, strict=True, view='model', **kwargs)

    def test_full_stays_compatible_model_omits_only_derivable_expansion(self):
        self.assertEqual(doctor_report(self.root, strict=True), self.report)
        page = self.prepare()
        self.assertFalse(page['healthy'])
        self.assertEqual(page['issueCounts'], self.report['issueCounts'])
        self.assertEqual(page['domains']['canon']['status'], 'semantic_review_required')
        self.assertNotIn('details', page['domains']['canon'])
        row = json.loads(page['issues'][0]['text'])
        self.assertEqual(row['current'], self.report['issues'])

    def test_comparison_groups_duplicate_identities_without_guessing_pairing(self):
        first = self.prepare()
        repeated = self.prepare(againstRef=first['doctorRef'])
        self.assertEqual(repeated['comparison']['groupCounts'], {'unchanged': 1})
        self.report['issues'] = [issue(message='Changed explanation'), issue(message='Second occurrence'), issue(code='NEW')]
        current = self.prepare(againstRef=first['doctorRef'])
        self.assertEqual(current['comparison']['groupCounts'], {'changed': 1, 'added': 1})
        self.report['issues'] = []
        removed = self.prepare(againstRef=first['doctorRef'])
        self.assertEqual(removed['comparison']['groupCounts'], {'no_longer_reported': 1})
        self.assertFalse(removed['comparison']['removalIsSemanticResolution'])
        self.assertFalse(removed['healthy'])

    def test_missing_baseline_scope_and_policy_changes_are_not_comparable(self):
        first = self.prepare()
        changed = doctor_report(self.root, strict=False, view='model', againstRef=first['doctorRef'])
        self.assertFalse(changed['comparison']['comparable'])
        with patch('scripts.core.doctor_delivery.toolbox_signature', return_value=[('policy', 'changed')]):
            changed = self.prepare(againstRef=first['doctorRef'])
        self.assertFalse(changed['comparison']['comparable'])
        missing = self.prepare(againstRef='gn:doctor-report:' + '0' * 32)
        self.assertTrue(missing['comparison']['requested'])
        self.assertFalse(missing['comparison']['comparable'])

    def test_identity_comes_from_source_not_the_project_domain_projection(self):
        self.report['project'] = {'templates': {'missing': []}}
        first = self.prepare()
        current = self.prepare(againstRef=first['doctorRef'])
        self.assertTrue(current['comparison']['comparable'])
        self.source['projectId'] = 'another-project'
        write_json(self.root / '.govel/structure.json', self.source)
        replaced = self.prepare(againstRef=first['doctorRef'])
        self.assertFalse(replaced['comparison']['comparable'])

    def test_long_issues_page_losslessly_without_rerunning_doctor(self):
        self.report['issues'][0]['evidence']['long'] = '证据\r\n' * 2000
        pages, cursor = [], None
        while True:
            page = self.prepare(maxBytes=3000, **({'cursor': cursor} if cursor else {}))
            pages.append(page)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()), 3000)
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        self.assertEqual(self.assess.call_count, 1)
        chunks = [row['text'] for page in pages for row in page['issues'] if row['id'].startswith('DI-')]
        self.assertEqual(json.loads(''.join(chunks))['current'], self.report['issues'])
        self.source['bookName'] += 'changed'; write_json(self.root / '.govel/structure.json', self.source)
        with self.assertRaises(StructureError):
            self.prepare(maxBytes=3000, cursor=pages[0]['nextCursor'])

    def test_error_envelope_retains_compact_blockers(self):
        with self.assertRaises(StructureError) as caught:
            call_operation('project.doctor', self.root, {'strict': True, 'view': 'model'})
        self.assertEqual(caught.exception.code, 'DOCTOR_UNHEALTHY')
        self.assertFalse(caught.exception.details['healthy'])
        self.assertTrue(caught.exception.details['doctorRef'])

    def test_mcp_defaults_to_model_but_honors_explicit_full(self):
        from scripts.mcp_server import handle
        def invoke(arguments):
            return handle({'id': 1, 'method': 'tools/call', 'params': {
                'name': 'doctor', 'arguments': {'projectRoot': str(self.root), **arguments}}})['result']
        compact = invoke({})
        self.assertTrue(compact['isError'])
        self.assertEqual(compact['structuredContent']['details']['view'], 'model')
        full = invoke({'view': 'full'})
        self.assertEqual(full['structuredContent']['details'], self.report)
