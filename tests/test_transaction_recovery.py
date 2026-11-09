from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.finalization import pending_transactions, recover_transactions
from scripts.core.locks import project_lock, project_lock_observation
from tests.fixtures_current import create_schema5_project
from tests.fixtures_transaction import interrupted_write_set


class TransactionRecoveryTests(unittest.TestCase):
    def test_interrupted_transaction_is_reported_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_schema5_project(root, chapter_count=1)
            target = root / "正文" / f"{fixture['chapterIds'][0]}.md"
            original = target.read_bytes()
            relative = target.relative_to(root).as_posix()
            interrupted_write_set(root, {relative: '被中断的新正文'.encode()}, phase='write', relative=relative)
            self.assertEqual(target.read_text(), '被中断的新正文')

            report = recover_transactions(root)
            self.assertEqual(report["code"], "TRANSACTION_INCOMPLETE")
            self.assertEqual(len(pending_transactions(root)), 1)
            recovered = recover_transactions(root, apply=True)
            self.assertEqual(recovered["code"], "TRANSACTION_RECOVERED")
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(pending_transactions(root), [])

    def test_idle_anchor_is_reused_without_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema5_project(root, chapter_count=1)
            lock = root / '.govel/structure.lock'
            original = lock.stat().st_ino
            with project_lock(root):
                self.assertTrue(project_lock_observation(root)['locked'])
            self.assertEqual(lock.stat().st_ino, original)
            self.assertFalse(project_lock_observation(root)['locked'])

    def test_committed_interruption_finishes_cleanup_without_reverting_new_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema5_project(root, chapter_count=1)
            target = root/'工作台/committed'
            target.write_bytes(b'old')
            interrupted_write_set(root, {'工作台/committed': b'new'}, phase='committed')
            self.assertEqual(pending_transactions(root)[0]['phase'], 'committed')
            recovered = recover_transactions(root, apply=True)
            self.assertEqual(recovered['code'], 'TRANSACTION_RECOVERED')
            self.assertEqual(target.read_bytes(), b'new')
            self.assertEqual(pending_transactions(root), [])


if __name__ == "__main__":
    unittest.main()
