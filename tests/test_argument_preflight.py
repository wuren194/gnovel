"""Collect independent input mistakes without invoking or authorizing the target operation."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.core.argument_validation import argument_errors, preflight_arguments
from scripts.core.operations import call_operation, operations, validate_arguments
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.structure_store import StructureError


def decode_errors(pages):
    chunks = {}
    for page in pages:
        for row in page['errors']:
            pieces = chunks.setdefault(row['id'], [])
            if row['start'] != sum(len(piece) for piece in pieces):
                raise AssertionError('noncontiguous error material')
            pieces.append(row['text'])
    return [json.loads(''.join(parts)) for parts in chunks.values()]


class ArgumentPreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.operation = 'source.fact-patch-preview'
        self.arguments = {'sourceHash': 7, 'patches': [
            {'recordId': 'plan-ONE', 'reason': '', 'replacements': []},
            {'recordId': 9, 'reason': 'explicit', 'replacements': [{'old': 'before', 'new': False}]},
        ], 'approvalActor': False, 'approvalEvidence': 'explicit'}

    def preflight(self, **options):
        return call_operation('capabilities.validate', self.root,
                              {'operation': self.operation,
                               **({} if 'validationRef' in options else {'arguments': self.arguments}), **options})

    def test_collects_errors_across_fields_and_items_using_the_dispatch_validator(self):
        page = self.preflight()
        errors = decode_errors([page])
        self.assertTrue(page['ok'])
        self.assertFalse(page['schemaValid'])
        self.assertFalse(page['executed'])
        self.assertFalse(page['approvalGranted'])
        self.assertEqual(page['coverage']['domainChecks'], 'not_run')
        self.assertEqual([row['path'] for row in errors], [
            'arguments.sourceHash', 'arguments.patches[0].reason', 'arguments.patches[0].replacements',
            'arguments.patches[1].recordId', 'arguments.patches[1].replacements[0].new', 'arguments.approvalActor'])
        self.assertEqual(page['errorCount'], len(errors))
        spec = next(spec for spec in operations() if spec.id == self.operation)
        with self.assertRaises(StructureError) as caught:
            validate_arguments(spec.input_schema, self.arguments)
        self.assertEqual(str(caught.exception), errors[0]['message'])
        self.assertEqual(caught.exception.details['path'], errors[0]['path'])

    def test_missing_and_extra_fields_do_not_hide_independent_supplied_field_errors(self):
        schema = {'type': 'object', 'required': ['missing'], 'additionalProperties': False,
                  'properties': {'missing': {'type': 'string'}, 'count': {'type': 'integer'},
                                 'rows': {'type': 'array', 'items': {'type': 'string'}}}}
        errors = list(argument_errors(schema, {'extra': True, 'count': False, 'rows': [4, 5]}))
        self.assertEqual(errors[0]['missing'], ['missing'])
        self.assertEqual(errors[0]['unknown'], ['extra'])
        self.assertEqual([row['path'] for row in errors[1:]], ['arguments.count', 'arguments.rows[0]', 'arguments.rows[1]'])

    def test_unresolved_union_and_wrong_parent_type_remain_explicit(self):
        schema = {'type': 'array', 'items': {'oneOf': [
            {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']},
            {'type': 'string', 'minLength': 2},
        ]}}
        self.assertEqual(list(argument_errors(schema, [{'value': 1}, 'ok'])), [])
        errors = list(argument_errors(schema, [{'value': False}, 'x', 7]))
        self.assertEqual([row['path'] for row in errors], ['arguments[0]', 'arguments[1]', 'arguments[2]'])
        self.assertTrue(all(row['descendantChecks'] == 'branch_shape_unresolved' for row in errors))
        self.assertTrue(all(row['alternatives'] for row in errors))
        malformed = list(argument_errors({'type': 'object', 'properties': {'x': {'type': 'integer'}}}, []))
        self.assertEqual(len(malformed), 1)
        self.assertEqual(malformed[0]['descendantChecks'], 'parent_type_invalid')

    def test_explicit_kind_reports_all_independent_fields_of_that_schema_branch(self):
        from scripts.core.operation_schemas import canon_proposal_item
        value = {'kind': 'fact', 'recordId': 3, 'patch': {}, 'reason': False, 'evidence': 'not-an-array'}
        errors = list(argument_errors(canon_proposal_item(), value, 'arguments.items[0]'))
        self.assertEqual({row['path'] for row in errors}, {
            'arguments.items[0].recordId', 'arguments.items[0].patch',
            'arguments.items[0].reason', 'arguments.items[0].evidence'})
        evidence = next(row for row in errors if row['path'].endswith('.evidence'))
        self.assertEqual(evidence['schemaBranch']['discriminator'], 'kind')
        with self.assertRaises(StructureError):
            validate_arguments(canon_proposal_item(), value)

    def test_boolean_schemas_keep_kind_specific_fields_closed(self):
        validate_arguments(True, {'anything': []})
        with self.assertRaises(StructureError):
            validate_arguments(False, 'value')
        schema = {'type': 'object', 'properties': {'blocked': False, 'free': True}}
        validate_arguments(schema, {'free': {'nested': [1]}})
        with self.assertRaises(StructureError) as caught:
            validate_arguments(schema, {'blocked': None})
        self.assertEqual(caught.exception.details['path'], 'arguments.blocked')
        from scripts.core.operation_schemas import canon_proposal_item
        item = {'kind': 'fact', 'recordId': 'fact-EXAMPLE', 'patch': [{'op': 'set', 'path': ['text'], 'value': 'new'}],
                'reason': 'explicit', 'evidence': [{'fragmentId': 'fact-EXAMPLE', 'quote': 'old'}]}
        validate_arguments(canon_proposal_item(), item)
        with self.assertRaises(StructureError):
            validate_arguments(canon_proposal_item(), {**item, 'evidenceMode': 'author_declaration'})

    def test_shared_record_patch_rejects_fields_from_another_operation(self):
        from scripts.core.operation_schemas import RECORD_PATCH
        for valid, invalid in (
                ({'op': 'set', 'path': ['label'], 'value': 'new'}, {'original': 'old'}),
                ({'op': 'remove', 'path': ['description']}, {'value': None}),
                ({'op': 'text', 'path': ['label'], 'original': 'old', 'replacement': 'new'}, {'value': 'extra'})):
            with self.subTest(op=valid['op']):
                validate_arguments(RECORD_PATCH, valid)
                with self.assertRaises(StructureError):
                    validate_arguments(RECORD_PATCH, {**valid, **invalid})
                for key in valid:
                    with self.subTest(missing=key), self.assertRaises(StructureError):
                        validate_arguments(RECORD_PATCH, {name: value for name, value in valid.items() if name != key})

    def test_discriminated_union_does_not_guess_missing_overlapping_or_unknown_tags(self):
        schema = {'oneOf': [
            {'type': 'object', 'properties': {'kind': {'const': 'a'}, 'x': {'type': 'integer'}}, 'required': ['kind', 'x']},
            {'type': 'object', 'properties': {'kind': {'const': 'b'}, 'x': {'type': 'string'}}, 'required': ['kind', 'x']},
        ]}
        for value in ({'kind': 'a', 'x': 1}, {'kind': 'b', 'x': 'ok'}):
            validate_arguments(schema, value)
            self.assertEqual(list(argument_errors(schema, value)), [])
        for value in ({'x': 1}, {'kind': 'unknown', 'x': 1}, {'kind': True, 'x': 1}):
            errors = list(argument_errors(schema, value))
            self.assertEqual(errors[0]['descendantChecks'], 'branch_shape_unresolved')
        overlapping = {'oneOf': [schema['oneOf'][0], deepcopy(schema['oneOf'][0])]}
        errors = list(argument_errors(overlapping, {'kind': 'a', 'x': 1}))
        self.assertEqual(errors[0]['descendantChecks'], 'branch_shape_unresolved')

    def test_boolean_constants_combinators_and_bounds_keep_strict_semantics(self):
        cases = [({'enum': [1]}, True), ({'const': True}, 1),
                 ({'allOf': [{'type': 'integer'}, {'minimum': 2}]}, 1),
                 ({'oneOf': [{'type': 'integer'}, {'minimum': 1}]}, 2),
                 ({'type': 'string', 'maxLength': 2}, 'long'),
                 ({'type': 'array', 'uniqueItems': True, 'maxItems': 1}, [1, 1])]
        for schema, value in cases:
            with self.subTest(schema=schema), self.assertRaises(StructureError):
                validate_arguments(schema, value)
            self.assertTrue(list(argument_errors(schema, value)))

    def test_valid_schema_does_not_call_handler_or_validate_project_and_approval(self):
        specs = operations()
        target = next(spec for spec in specs if spec.id == 'project.structure.commit')
        handler = Mock(side_effect=AssertionError('preflight invoked the target'))
        modified = [replace(spec, handler=handler) if spec.id == target.id else spec for spec in specs]
        sentinel = self.root / 'authored.txt'
        sentinel.write_text('original')
        with patch('scripts.core.operations.operations', return_value=modified):
            result = preflight_arguments(self.root, operation=target.id,
                                         arguments={'operationId': 'not-a-real-plan', 'planHash': 'not-a-real-hash', 'approve': True})
        self.assertTrue(result['schemaValid'], result)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['targetEffects'], 'protected-write')
        self.assertFalse(result['executed'])
        self.assertFalse(result['approvalGranted'])
        handler.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), 'original')

    def test_unknown_operations_and_bad_budgets_fail_without_dispatch(self):
        with self.assertRaises(StructureError) as caught:
            self.preflight(operation='os.system')
        self.assertEqual(caught.exception.code, 'OPERATION_NOT_FOUND')
        for options in ({'limit': 0}, {'maxBytes': 0}, {'maxBytes': 1}, {'limit': True}):
            with self.subTest(options=options), self.assertRaises(StructureError):
                self.preflight(**options)

    def test_long_unicode_error_material_is_paginated_without_truncation(self):
        self.arguments['未知字段🙂' * 700] = 1
        pages, cursor, reference = [], None, None
        while True:
            page = self.preflight(limit=2, maxBytes=2200,
                                  **({'cursor': cursor, 'validationRef': reference} if cursor else {}))
            pages.append(page)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()), 2200)
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
            reference = page['validationRef']
        errors = decode_errors(pages)
        spec = next(spec for spec in operations() if spec.id == self.operation)
        self.assertEqual(errors, list(argument_errors(spec.input_schema, {**self.arguments, 'projectRoot': str(self.root)})))
        self.assertGreater(len(pages), 1)

    def test_reference_is_frozen_project_bound_and_never_reconstructed_by_a_reader(self):
        first = self.preflight(limit=1)
        self.arguments = {}
        with patch('scripts.core.argument_validation.argument_errors', side_effect=AssertionError('revalidated frozen arguments')):
            following = preflight_arguments(self.root, operation=self.operation, validationRef=first['validationRef'],
                                             limit=1, cursor=first['nextCursor'])
        self.assertEqual(following['errorCount'], first['errorCount'])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(StructureError):
                preflight_arguments(Path(temp), operation=self.operation, validationRef=first['validationRef'])
            with patch('scripts.core.transport_cache._directory', return_value=Path(temp)), self.assertRaises(StructureError) as caught:
                self.preflight(validationRef=first['validationRef'])
        self.assertEqual(caught.exception.code, 'TRANSPORT_SNAPSHOT_UNAVAILABLE')
        with self.assertRaises(StructureError):
            preflight_arguments(self.root, operation=self.operation, arguments={}, validationRef=first['validationRef'])

    def test_reference_refuses_a_changed_validation_contract(self):
        first = self.preflight()
        specs = [replace(spec, input_schema={**spec.input_schema, 'description': 'changed'})
                 if spec.id == self.operation else spec for spec in operations()]
        with patch('scripts.core.operations.operations', return_value=specs), self.assertRaises(StructureError) as caught:
            preflight_arguments(self.root, operation=self.operation, validationRef=first['validationRef'])
        self.assertEqual(caught.exception.code, 'ARGUMENTS_PREFLIGHT_STALE')

    def test_cursor_binds_input_schema_and_query_budget(self):
        first = self.preflight(limit=1)
        cursor = first['nextCursor']
        self.assertIsNotNone(cursor)
        with self.assertRaises(StructureError) as caught:
            self.preflight(limit=2, cursor=cursor)
        self.assertEqual(caught.exception.code, 'CURSOR_QUERY_MISMATCH')
        self.arguments['sourceHash'] = 'changed'
        with self.assertRaises(StructureError) as caught:
            self.preflight(limit=1, cursor=cursor)
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')
        self.arguments['sourceHash'] = 7
        specs = operations()
        changed = []
        for spec in specs:
            schema = deepcopy(spec.input_schema)
            if spec.id == self.operation:
                schema['properties']['sourceHash']['description'] = 'changed input contract'
            changed.append(replace(spec, input_schema=schema))
        with patch('scripts.core.operations.operations', return_value=changed), self.assertRaises(StructureError) as caught:
            preflight_arguments(self.root, operation=self.operation, arguments=self.arguments, limit=1, cursor=cursor)
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def test_cli_mcp_and_facade_return_identical_preflight(self):
        path = self.root / 'arguments.json'
        path.write_text(json.dumps(self.arguments), encoding='utf-8')
        before = path.read_bytes()
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'tools', 'validate',
                              '--project-root', str(self.root), '--operation', self.operation, '--arguments', '@' + str(path)],
                             capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        from scripts.mcp_server import handle
        mcp = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'preflight_operation_arguments', 'arguments': {'projectRoot': str(self.root),
                'operation': self.operation, 'arguments': self.arguments}}})['result']
        self.assertFalse(mcp['isError'])
        self.assertEqual(json.loads(cli.stdout), mcp['structuredContent'])
        self.assertEqual(mcp['structuredContent'], self.preflight())
        self.assertEqual(path.read_bytes(), before)

    def test_query_policy_is_consumed_and_invalid_input_is_still_blocked_by_dispatch(self):
        from scripts.core.project import query_policy
        policy = query_policy()
        policy['defaultLimit'] = 1
        with patch('scripts.core.project.query_policy', return_value=policy):
            report = self.preflight()
        self.assertEqual(report['returned'], 1)
        self.assertTrue(report['hasMore'])
        with self.assertRaises(StructureError) as caught:
            call_operation(self.operation, self.root, self.arguments)
        self.assertEqual(caught.exception.code, 'ARGUMENTS_INVALID')


if __name__ == '__main__':
    unittest.main()
