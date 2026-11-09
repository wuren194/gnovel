from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import state_cache
from tests.fixtures_current import create_schema5_project


ROOT = Path(__file__).resolve().parents[1]


class CacheDependencyTests(unittest.TestCase):
    def test_manifest_declares_existing_cache_dependencies(self) -> None:
        manifest = json.loads((ROOT / "toolbox.manifest.json").read_text(encoding="utf-8"))
        dependencies = manifest["storage"]["cacheDependencies"]
        self.assertTrue(dependencies)
        for relative in dependencies:
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_declared_dependency_change_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema5_project(root, chapter_count=1)
            self.assertTrue(state_cache.is_fresh(root))
            dependency = ROOT / "scripts/core/prompt_plan.py"
            dependency.write_bytes(dependency.read_bytes() + b"\n")
            self.assertFalse(state_cache.is_fresh(root))



# Execute every toolbox mutation (including CLI children) in an owned physical copy.
from tests.toolbox_isolation import install_isolation
install_isolation(globals())

if __name__ == "__main__":
    unittest.main()
