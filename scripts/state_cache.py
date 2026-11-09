#!/usr/bin/env python3
"""Build strict schema-5 StoryState and StoryDesign projections."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

try:
    from scripts.core.clock import utc_now
    from scripts.core.jsonio import read_json, write_json
    from scripts.core.project import MANIFEST_PATH, TOOLBOX_ROOT
    from scripts.core.paths import ProjectPathError, safe_project_path
    from scripts.core.structure_store import (
        PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, StructureError, load_structure, ordered_chapters,
    )
except ImportError:
    from core.clock import utc_now
    from core.jsonio import read_json, write_json
    from core.project import MANIFEST_PATH, TOOLBOX_ROOT
    from core.paths import ProjectPathError, safe_project_path
    from core.structure_store import (
        PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, StructureError, load_structure, ordered_chapters,
    )

CACHE_DIR_REL = ".govel/cache"
STATE_REL = f"{CACHE_DIR_REL}/story-state.json"
DESIGN_REL = f"{CACHE_DIR_REL}/story-design.json"
MANIFEST_REL = f"{CACHE_DIR_REL}/manifest.json"
_MAX_ATTEMPTS = 3

class StateCacheError(ValueError):
    """Strict project structure or derived cache is invalid."""


def _content_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _snapshot(path: Path) -> dict:
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        digest = hashlib.sha256(handle.read()).hexdigest()
        after = os.fstat(handle.fileno())
    if (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
        raise OSError(f"source changed while reading: {path}")
    return {"sha256": digest}




def _source_snapshots(root: Path) -> dict[str, dict]:
    required = [
        root / ".govel" / "project.json",
        root / ".govel" / "structure.json",
    ]
    structure = load_structure(root)
    snapshots = {str(path.relative_to(root)): _snapshot(path) for path in required}
    for chapter in structure['chapters']:
        path = safe_project_path(root, chapter['filePath'])
        if path.is_file():
            snapshots[str(path.relative_to(root))] = _snapshot(path)
    return snapshots


def _parser_snapshot() -> dict:
    try:
        manifest = read_json(MANIFEST_PATH)
    except (OSError, json.JSONDecodeError):
        manifest = {}
    declared = manifest.get("storage", {}).get("cacheDependencies", [])
    dependencies = [TOOLBOX_ROOT / str(relative) for relative in declared]
    if Path(__file__).resolve() not in dependencies:
        dependencies.append(Path(__file__).resolve())
    return {
        str(path.relative_to(TOOLBOX_ROOT)) if path.is_relative_to(TOOLBOX_ROOT) else str(path): _snapshot(path)
        for path in dependencies
        if path.is_file()
    }


def _toolbox_version() -> str:
    try:
        manifest = read_json(MANIFEST_PATH)
    except (OSError, json.JSONDecodeError):
        return "unknown"
    return str(manifest.get("version", "unknown"))

def _project_marker(root: Path) -> dict:
    path = root / ".govel" / "project.json"
    try:
        marker = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateCacheError(f"project marker invalid: {exc}") from exc
    if marker.get("schemaVersion") != PROJECT_SCHEMA_VERSION or marker.get("workflowVersion") != WORKFLOW_VERSION:
        raise StateCacheError("项目格式不受运行时支持；请把旧项目当作外部小说，人工整理后迁入新项目")
    return marker


def _validate_paths(root: Path, structure: dict) -> None:
    missing = []
    for chapter in structure.get("chapters", []):
        # draft/review 的候选正文住在工作台工件；受保护 `正文/` 只由 finalize 写入。
        # 只有 stable/stale 已形成正式事实，才要求 filePath 对应文件存在。
        if chapter.get("status") not in {"stable", "stale"}:
            continue
        try:
            path = safe_project_path(root, chapter["filePath"])
        except ProjectPathError as exc:
            missing.append({"chapterId": chapter["chapterId"], "filePath": chapter["filePath"], "code": exc.code})
            continue
        if not path.is_file():
            missing.append({"chapterId": chapter["chapterId"], "filePath": chapter["filePath"]})
    if missing:
        raise StateCacheError(f"chapter body files missing: {missing}")


def project_projections(root: Path) -> tuple[dict, dict]:
    """Build both projections from one validated snapshot without publishing a cache."""
    root = root.expanduser().resolve()
    safe_project_path(root, ".govel/project.json")
    marker = _project_marker(root)
    structure = load_structure(root)
    if marker["projectId"] != structure["projectId"]:
        raise StateCacheError("project marker and structure projectId differ")
    _validate_paths(root, structure)
    return _build_state(marker, structure), _build_design(structure)


def build_state(root: Path, *, structure: dict | None = None) -> dict:
    """structure: a snapshot the caller already loaded and validated; it is not reread."""
    root = root.expanduser().resolve()
    marker = _project_marker(root)
    if structure is None:
        try:
            structure = load_structure(root)
        except StructureError as exc:
            raise StateCacheError(f"structure invalid: {exc}") from exc
    _validate_paths(root, structure)
    return _build_state(marker, structure)


def _build_state(marker: dict, structure: dict) -> dict:
    chapters = ordered_chapters(structure, include_archived=True)
    active = [item for item in chapters if item.get("status") != "archived"]
    return {
        "schemaVersion": PROJECT_SCHEMA_VERSION,
        "sourceModelVersion": structure.get("schemaVersion"),
        "projectId": marker["projectId"],
        "structureRevision": structure["revision"],
        "structureHash": _content_hash(structure),
        "chapters": chapters,
        "volumes": structure.get("volumes", []),
        "arcs": structure.get("arcs", []),
        "chapterArcLinks": structure.get("chapterArcLinks", []),
        "arcPhases": structure.get("arcPhases", []),
        "characterStates": [],
        "foreshadowing": [],
        "knowledgeStates": [],
        "pairStates": [],
        "decisions": [],
        "entities": structure.get("entities", []),
        "pairs": structure.get("pairs", []),
        "pairAssessment": structure.get("pairAssessment", {"status": "unassessed"}),
        "relations": structure.get("relations", []),
        "references": structure.get("references", []),
        "timeAnchors": [item for item in structure.get("entities", []) if item.get("entityType") == "time_anchor"],
        "evolutionLedger": [],
        "aliases": structure.get("aliases", []),
        "head": {"chapterId": active[-1]["chapterId"], "displayOrdinal": active[-1]["displayOrdinal"]} if active else None,
    }


def build_design(root: Path, *, structure: dict | None = None) -> dict:
    root = root.expanduser().resolve()
    _project_marker(root)
    if structure is None:
        structure = load_structure(root)
    return _build_design(structure)


def _build_design(structure: dict) -> dict:
    characters = [item for item in structure.get("entities", []) if item.get("entityType") == "character"]
    return {
        "schemaVersion": PROJECT_SCHEMA_VERSION,
        "sourceModelVersion": structure.get("schemaVersion"),
        "projectId": structure["projectId"],
        "structureRevision": structure["revision"],
        "structureHash": _content_hash(structure),
        "entities": structure.get("entities", []),
        "pairs": structure.get("pairs", []),
        "pairAssessment": structure.get("pairAssessment", {"status": "unassessed"}),
        "characters": [
            {
                "characterId": item["entityId"],
                "name": item.get("name", ""),
                "aliases": item.get("aliases", []),
                "status": item.get("status", "active"),
                "summary": item.get("summary", ""),
                "voiceProfile": item.get("voiceProfile", {}),
            }
            for item in characters
        ],
    }


def _cache_paths(root: Path) -> tuple[Path, Path, Path]:
    directory = root / CACHE_DIR_REL
    return directory / "story-state.json", directory / "story-design.json", directory / "manifest.json"


def _read_fresh(root: Path) -> tuple[dict, dict, dict] | None:
    state_path, design_path, manifest_path = _cache_paths(root)
    try:
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != PROJECT_SCHEMA_VERSION:
            return None
        sources, parser = _source_snapshots(root), _parser_snapshot()
        if manifest.get("sources") != sources or manifest.get("parser") != parser:
            return None
        state, design = read_json(state_path), read_json(design_path)
        if not isinstance(state, dict) or not isinstance(design, dict):
            return None
        outputs = {STATE_REL: _content_hash(state), DESIGN_REL: _content_hash(design)}
        if manifest.get("outputs") != outputs:
            return None
        generation = manifest.get("generation")
        if not isinstance(generation, dict) or set(generation) != {"projectId", "structureRevision", "structureHash"}:
            return None
        for projection in (state, design):
            if projection.get("schemaVersion") != PROJECT_SCHEMA_VERSION or any(projection.get(key) != value for key, value in generation.items()):
                return None
        # Validate the buffered objects, not paths that another publisher can replace.
        if sources != _source_snapshots(root) or parser != _parser_snapshot():
            return None
        return state, design, manifest
    except (OSError, ValueError, TypeError):
        return None


def is_fresh(root: Path) -> bool:
    return _read_fresh(root.expanduser().resolve()) is not None


def rebuild(root: Path) -> dict:
    root = root.expanduser().resolve()
    state_path, design_path, manifest_path = _cache_paths(root)
    for _ in range(_MAX_ATTEMPTS):
        try:
            sources, parser = _source_snapshots(root), _parser_snapshot()
            marker = _project_marker(root)
            try:
                structure = load_structure(root)
            except StructureError as exc:
                raise StateCacheError(f"structure invalid: {exc}") from exc
            if marker["projectId"] != structure["projectId"]:
                raise StateCacheError("project marker and structure projectId differ")
            _validate_paths(root, structure)
            # Mutations call rebuild while holding project_lock. Never acquire it here.
            state = _build_state(marker, structure)
            design = _build_design(structure)
            if sources != _source_snapshots(root) or parser != _parser_snapshot():
                continue
            manifest = {
                "schemaVersion": PROJECT_SCHEMA_VERSION,
                "builtAt": utc_now(),
                "toolboxVersion": _toolbox_version(),
                "sources": sources,
                "parser": parser,
                "generation": {key: state[key] for key in ("projectId", "structureRevision", "structureHash")},
                "outputs": {STATE_REL: _content_hash(state), DESIGN_REL: _content_hash(design)},
            }
            # Each replace is atomic, but the three files are not a transaction.
            # Bind hashes from memory before publication, never from mutable paths.
            write_json(state_path, state)
            write_json(design_path, design)
            write_json(manifest_path, manifest)
        except OSError:
            continue
        fresh = _read_fresh(root)
        if fresh is not None:
            state, design, manifest = fresh
            return {
                "ok": True,
                "statePath": STATE_REL,
                "designPath": DESIGN_REL,
                "sources": sorted(manifest["sources"]),
                "characters": len(design["characters"]),
                "chapters": len(state["chapters"]),
                "entities": len(state["entities"]),
                "relations": len(state["relations"]),
                "builtAt": manifest["builtAt"],
            }
    raise StateCacheError("cache rebuild could not publish a stable generation; sources changed or cache publication failed")


def status(root: Path) -> dict:
    root = root.expanduser().resolve()
    problems = []
    try:
        _project_marker(root)
        structure = load_structure(root)
        _validate_paths(root, structure)
    except (StateCacheError, StructureError) as exc:
        problems.append(str(exc))
    try:
        sources = _source_snapshots(root)
    except (OSError, ValueError) as exc:
        sources = {}
        problems.append(str(exc))
    fresh = is_fresh(root)
    return {
        "status": "invalid" if problems else "fresh" if fresh else "stale",
        "sources": sorted(sources),
        "cacheFresh": fresh,
        "problems": problems,
    }


def load(root: Path) -> tuple[dict, dict]:
    root = root.expanduser().resolve()
    _project_marker(root)
    for _ in range(_MAX_ATTEMPTS):
        fresh = _read_fresh(root)
        if fresh is not None:
            return fresh[0], fresh[1]
        rebuild(root)
    fresh = _read_fresh(root)
    if fresh is not None:
        return fresh[0], fresh[1]
    raise StateCacheError("cache load could not read a stable generation")


def state_path(root: Path) -> Path:
    load(root)
    return _cache_paths(root)[0]


def design_path(root: Path) -> Path:
    load(root)
    return _cache_paths(root)[1]
