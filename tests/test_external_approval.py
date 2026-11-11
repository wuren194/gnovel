"""Desktop confirmation boundaries; no test opens a real author dialog."""
from contextlib import ExitStack
import ctypes
import os
from pathlib import Path
import plistlib
import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

from scripts.core import external_approval as approval
from scripts.core.structure_store import StructureError


class ExternalConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.system = self.stack.enter_context(patch.object(approval.platform, 'system', return_value='Darwin'))
        self.session = self.stack.enter_context(patch.object(approval, '_macos_session', return_value={
            'kCGSSessionOnConsoleKey': True, 'kCGSessionLoginDoneKey': True,
            'kCGSSessionUserIDKey': os.geteuid(),
        }))
        self.process = self.stack.enter_context(patch.object(approval.subprocess, 'run',
            return_value=subprocess.CompletedProcess([], 0, '确认采用\n', '')))

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(StructureError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def linux(self):
        self.system.return_value = 'Linux'
        self.stack.enter_context(patch.dict(os.environ, {'DISPLAY': ':0'}, clear=True))
        self.stack.enter_context(patch.object(Path, 'is_file', return_value=True))
        self.stack.enter_context(patch.object(os, 'access', return_value=True))

    def test_macos_passes_untrusted_text_as_one_argument_and_uses_policy_timeout(self):
        message = '\n" & do shell script "touch /tmp/not-executed"\n$(command) <b>原文</b> \\'
        self.assertEqual(approval._confirm_on_device(message), 'macos-native-confirmation')
        args, kwargs = self.process.call_args
        self.assertEqual(args[0], ['/usr/bin/osascript', '-e', approval._DIALOG, message])
        self.assertNotIn(message, approval._DIALOG)
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['timeout'], approval._duration('externalConfirmationTimeoutSeconds'))

    def test_missing_locked_other_user_and_nonconsole_sessions_fail_before_dialog(self):
        valid = self.session.return_value
        for invalid in ({}, None, {**valid, 'CGSSessionScreenIsLocked': True},
                        {**valid, 'CGSSessionScreenIsLocked': 'unknown'},
                        {**valid, 'kCGSSessionOnConsoleKey': False},
                        {**valid, 'kCGSessionLoginDoneKey': False},
                        {**valid, 'kCGSSessionUserIDKey': os.geteuid() + 1}):
            with self.subTest(session=invalid):
                self.session.return_value = invalid
                error = self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval._confirm_on_device, 'test')
                self.assertTrue(error.details['retryable'])
                self.assertIn('解锁', error.details['recommendedAction'])
        self.process.assert_not_called()

    def test_session_probe_failure_is_actionable_without_opening_dialog(self):
        self.session.side_effect = OSError('framework unavailable')
        self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval._confirm_on_device, 'test')
        self.process.assert_not_called()

    def test_ssh_can_confirm_in_same_users_unlocked_console_session(self):
        with patch.dict(os.environ, {'SSH_CONNECTION': 'remote'}):
            self.assertEqual(approval._confirm_on_device('test'), 'macos-native-confirmation')
        self.process.assert_called_once()

    def test_macos_cancel_is_not_approval(self):
        self.process.return_value = subprocess.CompletedProcess([], 1, '', 'User canceled. (-128)')
        self.assertIsNone(approval._confirm_on_device('test'))

    def test_macos_unexpected_output_and_desktop_failure_do_not_approve(self):
        for result in (subprocess.CompletedProcess([], 0, 'other', ''),
                       subprocess.CompletedProcess([], 1, '', 'no window server')):
            with self.subTest(result=result):
                self.process.return_value = result
                self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval._confirm_on_device, 'test')

    def test_timeout_and_start_failure_are_bounded_errors(self):
        for exception, code in ((subprocess.TimeoutExpired('dialog', 1), 'APPROVAL_EXPIRED'),
                                (OSError('cannot start'), 'APPROVAL_CHANNEL_UNAVAILABLE')):
            with self.subTest(code=code):
                self.process.side_effect = exception
                error = self.assert_code(code, approval._confirm_on_device, 'test')
                self.assertTrue(error.details['retryable'])

    def test_linux_uses_fixed_zenity_plain_text_default_cancel_without_shell(self):
        self.linux()
        message = '--ok-label=forged\n<b>hash</b> " $(command)'
        self.assertEqual(approval._confirm_on_device(message), 'linux-zenity-confirmation')
        command = self.process.call_args.args[0]
        self.assertEqual(command[0], '/usr/bin/zenity')
        self.assertIn('--default-cancel', command)
        self.assertIn('--no-markup', command)
        self.assertEqual(command[-1], '--text=' + message)
        self.assertNotIn('shell', self.process.call_args.kwargs)
        self.session.assert_not_called()

    def test_linux_without_display_or_executable_never_starts_dialog(self):
        self.linux()
        with patch.dict(os.environ, {}, clear=True):
            self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval._confirm_on_device, 'test')
        with patch.object(Path, 'is_file', return_value=False):
            self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval._confirm_on_device, 'test')
        with patch.object(os, 'access', return_value=False):
            self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval._confirm_on_device, 'test')
        self.process.assert_not_called()

    def test_linux_wayland_only_session_can_confirm(self):
        self.linux()
        with patch.dict(os.environ, {'WAYLAND_DISPLAY': 'wayland-0'}, clear=True):
            self.assertEqual(approval._confirm_on_device('test'), 'linux-zenity-confirmation')

    def test_linux_cancel_and_display_error_cannot_approve(self):
        self.linux()
        self.process.return_value = subprocess.CompletedProcess([], 1, '', '')
        self.assertIsNone(approval._confirm_on_device('test'))
        for result in (subprocess.CompletedProcess([], 1, '', 'cannot open display'),
                       subprocess.CompletedProcess([], -15, '', ''),
                       subprocess.CompletedProcess([], 5, '', 'error')):
            with self.subTest(result=result):
                self.process.return_value = result
                self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval._confirm_on_device, 'test')

    def test_unsupported_platform_never_starts_dialog(self):
        self.system.return_value = 'Windows'
        self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval._confirm_on_device, 'test')
        self.process.assert_not_called()

    def plan(self):
        root = Path('/tmp/govel-neutral-approval').resolve()
        return root, {'operationId': 'OP-neutral', 'planHash': 'a' * 64,
            'proposedStructure': {'bookName': '测试作品', 'chapters': [{'chapterId': 'chapter-001', 'title': '测试章节'}]},
            'externalReviewMaterials': {'projectRoot': str(root), 'projectId': 'prop-neutral',
                'chapterId': 'chapter-001', 'bindingGeneration': 2, 'filePath': '正文/测试.md',
                'expectedContentDigest': 'sha256:' + 'b' * 64,
                'observedContentDigest': 'sha256:' + 'c' * 64,
                'observedObject': {'dev': 1, 'ino': 2}, 'H1': {}, 'H2': {'path': 'frozen.md'}}}

    def test_linux_issued_capability_records_backend_and_is_one_use(self):
        self.linux()
        root, plan = self.plan()
        token = approval.issue_local_approval(root, plan)
        self.addCleanup(approval.discard_local_approval, token)
        # The capability keeps the actual confirmed backend if the environment changes.
        self.system.return_value = 'Darwin'
        witness = approval.consume_local_approval(plan, token, root=root)
        self.assertEqual(witness['channel'], 'linux-zenity-confirmation')
        self.assert_code('APPROVAL_CAPABILITY_INVALID', approval.consume_local_approval, plan, token, root=root)

    def test_failed_confirmation_never_issues_a_token_and_identifies_retry_plan(self):
        root, plan = self.plan()
        before = set(approval._issued)
        self.session.return_value = {}
        error = self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval.issue_local_approval, root, plan)
        self.assertEqual(set(approval._issued), before)
        self.assertEqual(error.details['operationId'], plan['operationId'])
        self.assertEqual(error.details['planHash'], plan['planHash'])

    def test_boolean_confirmation_cannot_fake_a_backend(self):
        root, plan = self.plan()
        before = set(approval._issued)
        with patch.object(approval, '_confirm_on_device', return_value=True):
            self.assert_code('APPROVAL_CHANNEL_UNAVAILABLE', approval.issue_local_approval, root, plan)
        self.assertEqual(set(approval._issued), before)


class MacosSessionReadingTests(unittest.TestCase):
    def test_native_objects_are_released_after_success_or_invalid_plist(self):
        for payload in (plistlib.dumps({'kCGSSessionOnConsoleKey': True}), b'invalid'):
            with self.subTest(payload=payload):
                graphics, foundation = MagicMock(), MagicMock()
                graphics.CGSessionCopyCurrentDictionary.return_value = 11
                foundation.CFPropertyListCreateData.return_value = 22
                buffer = ctypes.create_string_buffer(payload)
                foundation.CFDataGetBytePtr.return_value = ctypes.addressof(buffer)
                foundation.CFDataGetLength.return_value = len(payload)
                with patch.object(approval.ctypes, 'CDLL', side_effect=[graphics, foundation]):
                    if payload == b'invalid':
                        with self.assertRaises(plistlib.InvalidFileException):
                            approval._macos_session()
                    else:
                        self.assertEqual(approval._macos_session(), {'kCGSSessionOnConsoleKey': True})
                self.assertEqual(foundation.CFRelease.call_args_list, [call(22), call(11)])


if __name__ == '__main__':
    unittest.main()
