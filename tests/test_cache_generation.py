from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import state_cache
from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_commands import commit_plan, preview_create_time_anchor
from scripts.core.structure_store import load_structure, save_structure
from tests.fixtures_current import initialize_project


class CacheGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        initialize_project(self.root, book_name='缓存代际测试')
        state_cache.rebuild(self.root)

    def _commit(self) -> None:
        plan = preview_create_time_anchor(self.root, name="新时间锚", absolute_time="2000-01-01")
        commit_plan(self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"], approved=True)
        self.assertEqual(load_structure(self.root)["revision"], 1)
        state_cache.rebuild(self.root)
        self.assertTrue(state_cache.is_fresh(self.root))

    def _interleave(self, worker, ready: threading.Event, resume: threading.Event) -> object:
        results, errors = [], []

        def run() -> None:
            try:
                results.append(worker())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, name="old-cache-reader", daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(10), "worker did not reach the gate")
            self._commit()
        finally:
            resume.set()
            thread.join(10)
        self.assertFalse(thread.is_alive(), "cache work deadlocked with structural commit")
        self.assertEqual(errors, [])
        return results[0]

    def _assert_current(self, projections=None) -> None:
        state, design = projections if projections is not None else state_cache.load(self.root)
        structure = load_structure(self.root)
        self.assertTrue(state_cache.is_fresh(self.root))
        for projection in (state, design):
            self.assertEqual(projection["schemaVersion"], 5)
            self.assertEqual(projection["structureRevision"], structure["revision"])
            self.assertEqual(projection["entities"], structure["entities"])
        self.assertEqual(state["structureHash"], design["structureHash"])

    def test_commit_between_projections_retries_one_shared_snapshot(self) -> None:
        ready, resume = threading.Event(), threading.Event()
        original_state, original_design = state_cache._build_state, state_cache._build_design
        observed = []

        def gated_state(marker, structure):
            result = original_state(marker, structure)
            if threading.current_thread().name == "old-cache-reader" and not ready.is_set():
                observed.append(("state", id(structure), result["structureRevision"]))
                ready.set()
                self.assertTrue(resume.wait(10))
            return result

        def observe_design(structure):
            if threading.current_thread().name == "old-cache-reader":
                observed.append(("design", id(structure), structure["revision"]))
            return original_design(structure)

        with patch.object(state_cache, "_build_state", side_effect=gated_state), patch.object(state_cache, "_build_design", side_effect=observe_design):
            result = self._interleave(lambda: state_cache.rebuild(self.root), ready, resume)
        self.assertTrue(result["ok"])
        self.assertEqual(observed[0][0], "state")
        self.assertEqual(observed[1][0], "design")
        self.assertEqual(observed[0][1:], observed[1][1:])
        self.assertEqual(observed[0][2], 0)
        self._assert_current()

    def test_old_publisher_overwriting_new_generation_is_never_fresh(self) -> None:
        ready, resume = threading.Event(), threading.Event()
        original_write = state_cache.write_json
        old_writes = []

        def gated_write(path, data):
            old_worker = threading.current_thread().name == "old-cache-reader"
            if old_worker and path.name == "story-state.json" and not ready.is_set():
                ready.set()
                self.assertTrue(resume.wait(10))
            original_write(path, data)
            revision = data.get("structureRevision", data.get("generation", {}).get("structureRevision"))
            if old_worker and revision == 0:
                old_writes.append((path.name, state_cache.is_fresh(self.root)))

        with patch.object(state_cache, "write_json", side_effect=gated_write):
            self._interleave(lambda: state_cache.rebuild(self.root), ready, resume)
        self.assertEqual(old_writes, [
            ("story-state.json", False), ("story-design.json", False), ("manifest.json", False),
        ])
        self._assert_current()

    def test_load_retries_when_files_change_after_reading_state(self) -> None:
        ready, resume = threading.Event(), threading.Event()
        original_read = state_cache.read_json

        def gated_read(path):
            result = original_read(path)
            if threading.current_thread().name == "old-cache-reader" and path.name == "story-state.json" and not ready.is_set():
                ready.set()
                self.assertTrue(resume.wait(10))
            return result

        with patch.object(state_cache, "read_json", side_effect=gated_read):
            projections = self._interleave(lambda: state_cache.load(self.root), ready, resume)
        self._assert_current(projections)

    def test_load_rechecks_sources_even_with_buffered_old_projections(self) -> None:
        ready, resume = threading.Event(), threading.Event()
        original_read = state_cache.read_json

        def gated_read(path):
            result = original_read(path)
            if threading.current_thread().name == "old-cache-reader" and path.name == "story-design.json" and not ready.is_set():
                ready.set()
                self.assertTrue(resume.wait(10))
            return result

        with patch.object(state_cache, "read_json", side_effect=gated_read):
            projections = self._interleave(lambda: state_cache.load(self.root), ready, resume)
        self._assert_current(projections)

    def test_corrupt_projection_is_rebuilt(self) -> None:
        for relative in (state_cache.STATE_REL, state_cache.DESIGN_REL):
            for payload in ("{", "null", "[]", '{"schemaVersion":5}', '\ufffd'):
                with self.subTest(relative=relative, payload=payload):
                    (self.root / relative).write_text(payload, encoding="utf-8")
                    self.assertFalse(state_cache.is_fresh(self.root))
                    self._assert_current()
            with self.subTest(relative=relative, payload="invalid-utf8"):
                (self.root / relative).write_bytes(b"\xff")
                self.assertFalse(state_cache.is_fresh(self.root))
                self._assert_current()
            with self.subTest(relative=relative, payload="missing"):
                (self.root / relative).unlink()
                self.assertFalse(state_cache.is_fresh(self.root))
                self._assert_current()

    def test_valid_json_content_damage_is_detected_without_revision_change(self) -> None:
        for relative in (state_cache.STATE_REL, state_cache.DESIGN_REL):
            with self.subTest(relative=relative):
                data = read_json(self.root / relative)
                data["entities"] = [{"entityId": "TA-CORRUPT", "entityType": "time_anchor"}]
                write_json(self.root / relative, data)
                self.assertFalse(state_cache.is_fresh(self.root))
                self._assert_current()

    def test_old_projection_cannot_borrow_new_manifest(self) -> None:
        old_state = read_json(self.root / state_cache.STATE_REL)
        self._commit()
        write_json(self.root / state_cache.STATE_REL, old_state)
        self.assertFalse(state_cache.is_fresh(self.root))
        self._assert_current()

    def test_partial_publication_failure_stays_stale_until_repaired(self) -> None:
        structure = load_structure(self.root)
        structure["revision"] += 1
        save_structure(self.root, structure)
        original_write = state_cache.write_json

        def fail_design(path, data):
            if path.name == "story-design.json":
                raise OSError("simulated publication failure")
            original_write(path, data)

        with patch.object(state_cache, "write_json", side_effect=fail_design):
            with self.assertRaises(state_cache.StateCacheError):
                state_cache.rebuild(self.root)
        self.assertFalse(state_cache.is_fresh(self.root))
        self._assert_current()

    def test_same_revision_different_structure_cannot_share_generation(self) -> None:
        old_state = read_json(self.root / state_cache.STATE_REL)
        structure = load_structure(self.root)
        structure["entities"].append({"entityId": "TA-NEW", "entityType": "time_anchor", "name": "同修订新实体"})
        save_structure(self.root, structure)
        state_cache.rebuild(self.root)
        self.assertEqual(old_state["structureRevision"], structure["revision"])
        write_json(self.root / state_cache.STATE_REL, old_state)
        self.assertFalse(state_cache.is_fresh(self.root))
        self._assert_current()

    def test_corrupt_or_unbound_manifest_is_rebuilt(self) -> None:
        path = self.root / state_cache.MANIFEST_REL
        for data in (None, [], {"schemaVersion": 5}):
            with self.subTest(data=data):
                write_json(path, data)
                self.assertFalse(state_cache.is_fresh(self.root))
                self._assert_current()
        for key in ("outputs", "generation"):
            with self.subTest(missing=key):
                manifest = read_json(path)
                manifest.pop(key)
                write_json(path, manifest)
                self.assertFalse(state_cache.is_fresh(self.root))
                self._assert_current()
        manifest = read_json(path)
        manifest["generation"]["structureRevision"] += 1
        write_json(path, manifest)
        self.assertFalse(state_cache.is_fresh(self.root))
        self._assert_current()

    def test_continuous_source_changes_fail_closed_after_bounded_retries(self) -> None:
        original_design = state_cache._build_design
        count = 0

        def change_source(structure):
            nonlocal count
            count += 1
            latest = read_json(self.root / ".gnovel/structure.json")
            latest["revision"] += 1
            write_json(self.root / ".gnovel/structure.json", latest)
            return original_design(structure)

        with patch.object(state_cache, "_build_design", side_effect=change_source):
            with self.assertRaisesRegex(state_cache.StateCacheError, "stable generation"):
                state_cache.rebuild(self.root)
        self.assertEqual(count, state_cache._MAX_ATTEMPTS)
        self.assertFalse(state_cache.is_fresh(self.root))
        self._assert_current()

    def test_parser_changes_during_build_fail_closed(self) -> None:
        counter = 0

        def changing_parser():
            nonlocal counter
            counter += 1
            return {"parser.py": {"sha256": str(counter), "mtime": counter}}

        with patch.object(state_cache, "_parser_snapshot", side_effect=changing_parser):
            with self.assertRaisesRegex(state_cache.StateCacheError, "stable generation"):
                state_cache.rebuild(self.root)
            self.assertFalse(state_cache.is_fresh(self.root))
        self._assert_current()

    def test_source_json_cannot_be_excluded_by_generated_text(self) -> None:
        structure = load_structure(self.root)
        structure["bookName"] = "> GENERATED: not a derived structure"
        save_structure(self.root, structure)
        state_cache.rebuild(self.root)
        manifest = read_json(self.root / state_cache.MANIFEST_REL)
        self.assertIn(".gnovel/structure.json", manifest["sources"])
        structure["bookName"] += " changed"
        save_structure(self.root, structure)
        self.assertFalse(state_cache.is_fresh(self.root))
        self._assert_current()

    def test_public_builders_keep_schema5_api(self) -> None:
        state = state_cache.build_state(self.root)
        design = state_cache.build_design(self.root)
        self.assertEqual(state["schemaVersion"], 5)
        self.assertEqual(design["schemaVersion"], 5)
        self.assertEqual(state["structureHash"], design["structureHash"])
        self.assertEqual(state_cache.status(self.root)["status"], "fresh")
        (self.root / ".gnovel/structure.json").unlink()
        self.assertFalse(state_cache.is_fresh(self.root))
        self.assertEqual(state_cache.status(self.root)["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
