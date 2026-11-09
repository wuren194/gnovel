from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.novelctl import _initialize, project_doctor, NovelError
from scripts.core.structure_store import load_structure
from scripts.core.project import current_schema_version

ROOT = Path(__file__).resolve().parents[1]


class SourceInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = dict(project_id='new-source', book_name='新书', idempotency_key='init', approval_mode='manual')

    def test_initialization_and_retry_use_only_current_source(self):
        _, created, templates = _initialize(self.root, **self.args)
        self.assertTrue(created)
        self.assertEqual(templates, [])
        source = load_structure(self.root)
        self.assertEqual(source['schemaVersion'], current_schema_version())
        self.assertIn('fragments', source)
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertFalse(_initialize(self.root, **self.args)[1])
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_real_quick_init_cli_installs_source_specific_project_instructions(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'quick-init',
                                 '--project-root', str(self.root), '--book-name', '新源项目'],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)['doctor']['healthy'])
        self.assertEqual((self.root / 'AGENTS.md').read_text(), (ROOT / 'shared/templates/agents-template.md').read_text())
        self.assertEqual(list(self.root.rglob('*.md')), [self.root / 'AGENTS.md'])

    def test_nonempty_project_is_not_implicitly_migrated(self):
        original = self.root / '旧设定.md'; original.write_text('旧项目原文')
        with self.assertRaises(NovelError): _initialize(self.root, **self.args)
        self.assertEqual(original.read_text(), '旧项目原文')
        self.assertFalse((self.root / '.gnovel').exists())
