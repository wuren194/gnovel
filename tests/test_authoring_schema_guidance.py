"""Clients receive actual authoring shapes without guessing source internals."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import mcp_server, novelctl
from scripts.core.argument_validation import validate_arguments
from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifact_templates import artifact_guide, validate_artifact_schema_declarations
from scripts.core.authoring_forms import review_form
from scripts.core.behavior_review import COVERAGE_STATUSES, DOMAINS, coverage_schema, validate_coverage
from scripts.core.chapter_contract import MODES, contract_schema, source_contract, validate_contract
from scripts.core.jsonio import write_json
from scripts.core.operations import operations
from scripts.core.project import query_policy
from scripts.core.source_model import source_schema
from scripts.core.structure_store import StructureError, validate_structure
from scripts.core.tool_catalog import list_capabilities
from tests.fixtures_current import create_schema5_project


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


class AuthoringSchemaGuidanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'book'
        self.fixture = create_schema5_project(self.root, chapter_count=1)
        self.source = self.fixture['structure']
        self.chapter_id = self.fixture['chapterIds'][0]

    def files(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = novelctl.main(list(arguments))
        self.assertFalse(err.getvalue(), err.getvalue())
        return code, json.loads(out.getvalue())

    def mcp(self, name, arguments):
        return mcp_server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': name, 'arguments': arguments}})['result']

    def test_cli_and_mcp_deliver_same_authoritative_contract_shape(self):
        before = self.files()
        code, cli = self.cli('project', 'source', 'schema', '--project-root', str(self.root))
        mcp = self.mcp('get_source_schema', {'projectRoot': str(self.root)})
        self.assertEqual(code, 0)
        self.assertFalse(mcp['isError'], mcp)
        expected = contract_schema()
        self.assertEqual(cli['chapterContract']['inputSchema'], expected)
        self.assertEqual(mcp['structuredContent']['chapterContract']['inputSchema'], expected)
        operation = next(row for row in operations() if row.id == 'workflow.chapter-preview')
        self.assertEqual(operation.input_schema['properties']['chapterContract'], expected)
        self.assertEqual(expected['properties']['mode']['enum'], list(MODES))
        self.assertEqual(expected['properties']['behaviorCoverage'], coverage_schema())
        self.assertEqual(self.files(), before)

    def test_modes_match_source_extraction_and_reject_unknown_values(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                contract = deepcopy(self.source['fragments'][0]['chapterContract'])
                contract['mode'] = mode
                self.assertEqual(validate_contract(contract), [])
                validate_arguments(contract_schema(), contract)
                source = deepcopy(self.source)
                source['fragments'][0]['chapterContract'] = contract
                self.assertEqual(source_contract(source, self.chapter_id)['mode'], mode)
        for mode in ('buildup', 'peakscene', '剧情', '', None):
            with self.subTest(mode=mode):
                contract = {'targetRange': [1, 20], 'mode': mode}
                self.assertIn('CHAPTER_CONTRACT_MODE_INVALID', validate_contract(contract))
                with self.assertRaises(StructureError):
                    validate_arguments(contract_schema(), contract)

    def test_preview_reports_allowed_mode_before_writing_a_plan_on_both_transports(self):
        arguments = {'projectRoot': str(self.root), 'requestId': 'invalid-mode', 'title': '交班',
                     'volumeId': self.fixture['volumeId'], 'primaryArcId': self.fixture['arcId'],
                     'outlineText': '主角交还钥匙。', 'reason': '核对输入形状。',
                     'planField': 'plot.plan', 'chapterContract': {'targetRange': [1, 30], 'mode': 'buildup'}}
        before = self.files()
        result = self.mcp('preview_chapter_with_plan', arguments)
        self.assertTrue(result['isError'])
        error = result['structuredContent']
        self.assertEqual(error['code'], 'ARGUMENTS_INVALID')
        self.assertEqual(error['details']['path'], 'arguments.chapterContract.mode')
        self.assertEqual(error['details']['allowed'], list(MODES))
        code, error = self.cli('project', 'workflow', 'chapter-preview', '--project-root', str(self.root),
            '--request-id', 'invalid-mode', '--title', arguments['title'], '--volume-id', arguments['volumeId'],
            '--primary-arc-id', arguments['primaryArcId'], '--outline-text', arguments['outlineText'],
            '--reason', arguments['reason'], '--plan-field', 'plot.plan', '--chapter-contract',
            json.dumps(arguments['chapterContract']))
        self.assertEqual(code, 1)
        self.assertEqual(error['details']['allowed'], list(MODES))
        self.assertEqual(self.files(), before)

    def test_invalid_source_errors_explain_existing_attributes_and_mode_without_mutating(self):
        source = deepcopy(self.source)
        fragment = source['fragments'][0]
        fragment['contentBindings'] = []
        fragment['chapterContract']['mode'] = 'buildup'
        unchanged = deepcopy(source)
        with self.assertRaises(StructureError) as caught:
            validate_structure(source)
        errors = {row['code']: row for row in caught.exception.details['errors']}
        bindings = errors['SOURCE_BINDINGS_REQUIRED']
        self.assertEqual(bindings['field'], 'contentBindings')
        self.assertEqual(bindings['expected']['items'], 'existing attributeId')
        self.assertEqual(bindings['schemaAction']['operation'], 'source.schema')
        mode = errors['CHAPTER_CONTRACT_MODE_INVALID']
        self.assertEqual(mode['path'], 'fragments.' + fragment['fragmentId'] + '.chapterContract')
        self.assertEqual(mode['allowed'], list(MODES))
        self.assertEqual(source, unchanged)

    def test_nested_coverage_schema_is_exact_and_cannot_approve_blank_reviews(self):
        schema = coverage_schema()
        self.assertEqual(set(schema['required']), set(DOMAINS))
        self.assertIs(schema['additionalProperties'], False)
        for status in COVERAGE_STATUSES:
            value = {domain: status for domain in DOMAINS}
            validate_arguments(schema, value)
            self.assertTrue(validate_coverage(value))
        for value in ({}, {'relationship': 'applicable'}, {'relationship': 'none', 'living': 'not_applicable'},
                      {'relationship': 'not_applicable', 'living': 'not_applicable', 'extra': True}):
            with self.subTest(value=value), self.assertRaises(StructureError):
                validate_arguments(schema, value)
            self.assertFalse(validate_coverage(value))
        form = review_form(self.root, chapterId=self.chapter_id, artifactType='stage_a_review', view='full')
        self.assertEqual(form['guide']['fieldSchemas']['behaviorCoverage'], schema)
        self.assertEqual(form['payload']['decision'], 'pending')
        before = self.files()
        form['payload']['decision'] = 'adopt'
        with self.assertRaises(ValueError):
            pack_artifact(self.root, chapterId=self.chapter_id, artifactType='stage_a_review', payload=form['payload'])
        self.assertEqual(self.files(), before)

    def test_incomplete_coverage_error_distinguishes_shape_from_completion(self):
        from scripts.core.behavior_review import COMPLETED_COVERAGE_STATUSES, validate_completion
        from scripts.core.review_artifacts import ReviewArtifactError
        for status in COVERAGE_STATUSES - COMPLETED_COVERAGE_STATUSES:
            payload = {'behaviorCoverage': {domain: status for domain in DOMAINS}}
            with self.subTest(status=status), self.assertRaises(ReviewArtifactError) as caught:
                validate_completion(self.source, self.chapter_id, payload, 'stage_a_review', '')
            self.assertEqual(caught.exception.code, 'BEHAVIOR_COVERAGE_REQUIRED')
            self.assertNotIn(status, caught.exception.details['completionAllowed'])
            self.assertEqual(set(caught.exception.details['allowed']), COVERAGE_STATUSES)

    def test_guide_delivers_real_nested_fields_and_mechanical_responsibility(self):
        from scripts.core.artifact_bindings import BINDABLE_FIELDS
        from scripts.core.artifact_templates import SCHEMA_PATH
        definitions = json.loads(SCHEMA_PATH.read_text())['$defs']
        for kind in ('stage_a_review', 'stage_b_review'):
            with self.subTest(kind=kind):
                guide = artifact_guide(kind)
                for field in ('behaviorReviews', 'relationshipCausalityReviews'):
                    self.assertEqual(guide['fieldSchemas'][field], definitions[kind]['properties'][field])
                self.assertEqual(guide['toolOwnedFields'], sorted(BINDABLE_FIELDS[kind]))
                self.assertEqual(guide['formAction']['operation'], 'workflow.review-form')
                self.assertEqual(guide['formAction']['arguments'], {'artifactType': kind})
                self.assertEqual(guide['formAction']['requiredInputs'], ['chapterId'])

    def test_coverage_schema_drift_is_detected_by_actual_consistency_gate(self):
        from scripts.core import artifact_templates
        original = artifact_templates.read_json

        def changed(path):
            result = original(path)
            if path == artifact_templates.SCHEMA_PATH:
                result['$defs']['stage_a_review']['properties']['behaviorCoverage']['required'] = ['relationship']
            return result

        with patch.object(artifact_templates, 'read_json', side_effect=changed):
            report = validate_artifact_schema_declarations()
        self.assertFalse(report['ok'])
        self.assertIn('stage_a_review.behaviorCoverage input shape differs from runtime', report['problems'])

    def test_expanded_guidance_stays_inside_existing_response_budgets(self):
        policy = query_policy()['maxBytes']
        self.assertLessEqual(size(source_schema()), policy['schema'])
        self.assertLessEqual(size(list_capabilities(operation='workflow.chapter-preview')), policy['detail'])
        for kind in ('stage_a_review', 'stage_b_review'):
            self.assertLessEqual(size(artifact_guide(kind)), policy['detail'])
        form = review_form(self.root, chapterId=self.chapter_id, artifactType='stage_a_review')
        self.assertLessEqual(size(form), policy['detail'])


if __name__ == '__main__':
    unittest.main()
