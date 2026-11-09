from __future__ import annotations

import unittest

from scripts.benchmark_single_source import benchmark_prose, benchmark_source


class SourceScaleHarnessTests(unittest.TestCase):
    def test_small_source_fixture_checks_export_query_and_transaction(self):
        result = benchmark_source(0.05)
        self.assertTrue(all(result['correctness'].values()))
        self.assertGreater(result['sourceBytes'], 0)
        self.assertEqual(result['revisionAfterCommit'], 1)

    def test_small_prose_fixture_checks_full_and_changed_chapter_scans(self):
        result = benchmark_prose(8000)
        self.assertTrue(all(result['correctness'].values()))
        self.assertEqual(result['chapterCount'], 2)


if __name__ == '__main__':
    unittest.main()
