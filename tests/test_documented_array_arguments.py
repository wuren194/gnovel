"""Parse selected documented argv only; never dispatch their operations."""
from pathlib import Path
import shlex
import unittest

from scripts.novelctl import build_parser


ROOT = Path(__file__).resolve().parents[1]


class DocumentedArrayArgumentTests(unittest.TestCase):
    def parse_example(self, path, command):
        prefix = 'python3 scripts/novelctl.py ' + command + ' '
        lines = [line for line in (ROOT / path).read_text().splitlines() if line.startswith(prefix)]
        self.assertEqual(len(lines), 1, 'selected documented example must exist exactly once')
        return build_parser().parse_args(shlex.split(lines[0])[2:])

    def test_credential_read_batch_example_selects_both_read_ids(self):
        args = self.parse_example('docs/OPERATIONS.md', 'project task read-batch --project-root /path/to/书 --credential')
        self.assertEqual(args.operation_binding, 'task.read-batch')
        self.assertEqual(args.readIds, ['r0', 'r1'])

    def test_source_reviews_example_has_three_status_values(self):
        args = self.parse_example('docs/SINGLE-SOURCE.md', 'project source reviews')
        self.assertEqual(args.operation_binding, 'source.reviews')
        self.assertEqual(args.statuses, ['pending', 'stale', 'unreviewed'])
