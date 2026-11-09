"""Publication checks cover identifiers, archives and deleted historical files."""
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import zipfile

from scripts.release_audit import audit, audit_zip, audit_history


class ReleaseAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Example Author', '-c', 'user.email=example@invalid.test',
                               *args], cwd=self.root, text=True, capture_output=True, check=True)

    def test_identifier_parts_do_not_combine_into_a_review_word(self):
        path = self.root / 'example.py'
        path.write_text('maxExample = 1\n')
        self.assertTrue(audit(self.root, ['axe'])['ok'])
        path.write_text('sampleAxe = 1\n')
        report = audit(self.root, ['axe'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['problems'][0]['termIndexes'], [0])

    def test_cjk_terms_and_normalized_forms_remain_searchable(self):
        (self.root / 'note.txt').write_text('普通示例包含审阅信号。\nＭＡＲＫＥＲ\n')
        report = audit(self.root, ['审阅信号', 'marker'])
        self.assertEqual(report['problems'][0]['termIndexes'], [0, 1])

    def test_word_in_path_and_external_link_fail(self):
        (self.root / 'marker.py').write_text('ordinary = True\n')
        (self.root / 'alias').symlink_to(self.root / 'marker.py')
        report = audit(self.root, ['marker'])
        self.assertEqual({row['code'] for row in report['problems']}, {'REVIEW_TERM', 'SYMLINK_NOT_ALLOWED'})

    def test_zip_is_checked_without_worktree_exclusions(self):
        archive = self.root / 'release.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('dist/notes.txt', 'marker')
            link = zipfile.ZipInfo('link')
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(link, '../external')
        report = audit_zip(archive, ['marker'])
        self.assertFalse(report['ok'])
        self.assertEqual({row['code'] for row in report['problems']},
                         {'REVIEW_TERM', 'UNEXPECTED_ARCHIVE_PATH', 'SYMLINK_NOT_ALLOWED'})

    def test_deleted_text_in_git_history_still_blocks(self):
        self.git('init', '-q')
        path = self.root / 'old.txt'
        path.write_text('marker\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Initial example')
        path.unlink()
        self.git('add', '-u')
        self.git('commit', '-qm', 'Remove example')
        self.assertTrue(audit(self.root, ['marker'])['ok'])
        report = audit_history(self.root, ['marker'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['commitCount'], 2)
        self.assertEqual(report['problems'][0]['path'], 'old.txt')

    def test_clean_commit_can_be_audited(self):
        self.git('init', '-q')
        (self.root / 'example.py').write_text('ordinary = True\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Initial example')
        report = audit_history(self.root, ['marker'])
        self.assertTrue(report['ok'])
        self.assertEqual(report['fileVersions'], 1)


if __name__ == '__main__':
    unittest.main()
