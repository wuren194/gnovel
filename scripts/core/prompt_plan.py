"""Resolve manifest prompt plans into auditable, content-addressed load plans."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .artifacts import sha256_json, sha256_text
from .static_cache import cached_bytes, cached_sha256 as file_sha256
from .review_plan import DECISION_TYPES, SOURCE_CONTRACT, resolve_effective_review_plan
from .project import TOOLBOX_ROOT, toolbox_manifest
from .prompt_conditions import PromptPlanError, evaluate_condition, resolve_field


def _project_design(project_root: Path, chapter_id: str | None = None) -> dict[str, Any]:
    try:
        from scripts import state_cache
    except ImportError:
        import state_cache
    try:
        state, data = state_cache.project_projections(project_root)
    except state_cache.StateCacheError as exc:
        raise PromptPlanError("DESIGN_INVALID", str(exc)) from exc
    if not isinstance(data, dict):
        raise PromptPlanError("DESIGN_INVALID", "story design must be an object")
    if chapter_id is not None:
        if not any(row.get('chapterId') == chapter_id for row in state.get('chapters', [])):
            raise PromptPlanError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
        from .prompt_conditions import chapter_condition_design
        scoped = chapter_condition_design(state, chapter_id)
        data = {**data, 'chapter': scoped['chapter'], 'pairScopeIds': scoped['pairScopeIds']}
    return data


def _read_ranges(path: Path, selectors: list[str] | None) -> tuple[list[dict[str, Any]], int]:
    text = cached_bytes(path).decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    lines = text.splitlines(keepends=True)
    if selectors is None:
        return [{"startLine": 1, "endLine": len(lines), "sha256": sha256_text(text)}], len(text)
    if not isinstance(selectors, list) or not selectors or any(not isinstance(item, str) for item in selectors):
        raise PromptPlanError("PROMPT_SLICE_INVALID", f"invalid slice declaration: {path.name}")
    ranges = []
    selected: set[int] = set()
    for name in selectors:
        starts = [index for index, line in enumerate(lines) if line.strip() == f"<!-- prompt:{name} -->"]
        ends = [index for index, line in enumerate(lines) if line.strip() == f"<!-- /prompt:{name} -->"]
        if len(starts) != 1 or len(ends) != 1 or starts[0] + 1 >= ends[0]:
            raise PromptPlanError("PROMPT_SLICE_MISSING", f"missing or ambiguous slice {name}: {path.name}")
        indexes = set(range(starts[0] + 1, ends[0]))
        if selected & indexes:
            raise PromptPlanError("PROMPT_SLICE_OVERLAP", f"overlapping slice {name}: {path.name}")
        selected.update(indexes)
        content = "".join(lines[index] for index in sorted(indexes))
        ranges.append({"slice": name, "startLine": starts[0] + 2, "endLine": ends[0], "sha256": sha256_text(content)})
    return sorted(ranges, key=lambda row: row["startLine"]), sum(len(lines[index]) for index in selected)


def _reference(relative: str, view: str) -> dict[str, Any]:
    path = TOOLBOX_ROOT / relative
    if not path.exists():
        raise PromptPlanError("PROMPT_REFERENCE_MISSING", f"on-demand reference missing: {relative}")
    if path.is_file():
        return {"path": relative, "sha256": file_sha256(path)}
    if view == "full":
        return {"path": relative, "files": [
            {"path": str(item.relative_to(TOOLBOX_ROOT)), "sha256": file_sha256(item)}
            for item in sorted(path.rglob("*")) if item.is_file()
        ]}
    entries = [path / "manifest.json"] if (path / "manifest.json").is_file() else [path / "README.md"]
    return {"path": relative, "kind": "on-demand-directory", "entrypoints": [
        {"path": str(item.relative_to(TOOLBOX_ROOT)), "sha256": file_sha256(item)} for item in entries if item.is_file()
    ], "load": "按当前任务选择切片，不递归读取整库"}


def resolve_prompt_plan(
    project_root: Path,
    station: str,
    *,
    design: dict[str, Any] | None = None,
    decision_type: str | None = None,
    chapter_id: str | None = None,
    view: str = "full",
    review_class: str | None = None,
) -> dict[str, Any]:
    if view not in {"full", "station"}:
        raise PromptPlanError("PROMPT_VIEW_INVALID", f"unsupported prompt view: {view}")
    manifest = toolbox_manifest()
    plan = manifest.get("promptPlan", {}).get(station)
    if not isinstance(plan, dict):
        raise PromptPlanError("STATION_NOT_FOUND", f"unknown prompt plan station: {station}")
    design = design if design is not None else _project_design(project_root, chapter_id)
    if review_class is not None:
        if review_class not in {"ordinary", "heavy"} or decision_type != "chapter" or not isinstance(design.get("chapter"), dict):
            raise PromptPlanError("PROMPT_REVIEW_CLASS_INVALID", "review class requires a selected chapter decision")
        design = {**design, "chapter": {**design["chapter"], "reviewClass": review_class}}
    warnings: list[dict[str, Any]] = []
    loaded: list[dict[str, Any]] = []
    from .data_provider import roundtable_resource, binding, materials, ProviderError
    initial_provider = binding()
    uses_roundtable = (Path(SOURCE_CONTRACT).name in plan.get('always', [])
                      or any(row.get('contract') == Path(SOURCE_CONTRACT).name for row in plan.get('conditional', [])))
    private_roundtable = roundtable_resource() if uses_roundtable else None
    for name in plan.get("always", []):
        if private_roundtable is not None and name == Path(SOURCE_CONTRACT).name:
            continue
        path = TOOLBOX_ROOT / "shared" / "contracts" / name
        if not path.is_file():
            raise PromptPlanError("PROMPT_PATH_MISSING", f"contract path missing: {name}")
        loaded.append({"path": str(path.relative_to(TOOLBOX_ROOT)), "kind": "always", "sha256": file_sha256(path)})
    conditions: list[dict[str, Any]] = []
    for entry in plan.get("conditional", []):
        matched, evidence = evaluate_condition(design, entry.get("when", {}))
        missing_chapter = evidence["field"].removeprefix('design.').startswith("chapter.") and not isinstance(design.get("chapter"), dict)
        if evidence['status'] == 'unresolved':
            warnings.append({"code": "PROMPT_CHAPTER_REQUIRED" if missing_chapter else 'PROMPT_CONDITION_UNRESOLVED',
                             "contract": entry.get("contract"), 'field': evidence['field'],
                             'unresolvedInputs': evidence['unresolvedInputs'],
                             "message": "条件资料尚未完整评估；缺失或未知不能视为不适用"})
        item = {"contract": entry.get("contract"), "reason": entry.get("reason", ""), "matched": matched, "evidence": evidence}
        conditions.append(item)
        if matched:
            if private_roundtable is not None and entry.get('contract') == Path(SOURCE_CONTRACT).name:
                continue
            path = TOOLBOX_ROOT / "shared" / "contracts" / str(entry.get("contract"))
            if not path.is_file():
                raise PromptPlanError("PROMPT_PATH_MISSING", f"conditional contract path missing: {entry.get('contract')}")
            loaded.append({"path": str(path.relative_to(TOOLBOX_ROOT)), "kind": "conditional", "sha256": file_sha256(path), "evidence": evidence})
    references = manifest.get("onDemandReferences", {}).get(station, [])
    slices = manifest.get("promptSlices", {}).get(station, {})
    selectors = slices.get(decision_type, slices.get("default", {}))
    if not isinstance(selectors, dict):
        raise PromptPlanError("PROMPT_SLICE_INVALID", "station slices must be a contract mapping")
    declared_contracts = set(plan.get("always", [])) | {row["contract"] for row in plan.get("conditional", [])}
    if set(selectors) - declared_contracts:
        raise PromptPlanError("PROMPT_SLICE_INVALID", "slice references a contract outside the station plan")
    contract_characters = 0
    for row in loaded:
        path = TOOLBOX_ROOT / row["path"]
        ranges, characters = _read_ranges(path, selectors.get(path.name))
        row["readRanges"] = ranges
        contract_characters += characters
    result = {
        "schemaVersion": 1,
        "view": view,
        "station": station,
        "projectId": design.get("projectId"),
        "chapterId": design.get("chapter", {}).get("chapterId"),
        "complete": not warnings,
        "warnings": warnings,
        "contracts": loaded,
        'roundtable': ({'source': 'user' if private_roundtable else 'default',
                       'label': '用户圆桌' if private_roundtable else '通用圆桌',
                       'resource': private_roundtable} if uses_roundtable else None),
        "conditional": conditions,
        "conditionInputsHash": sha256_json([row['evidence']['inputHash'] for row in conditions]),
        "onDemandReferences": [_reference(relative, view) for relative in references],
        "characterCounts": {"contractText": contract_characters, "response": 0, "unit": "unicodeCharacters"},
    }
    result['provider'] = initial_provider
    result['providerMaterials'] = materials(station.removeprefix('govel-'), design)
    for row in result['providerMaterials']:
        if not row['active'] or (row['condition'] or {}).get('status') == 'unresolved':
            result['complete'] = False
            warnings.append({'code': 'PROVIDER_CONDITION_UNRESOLVED', 'materialId': row['id'],
                             'message': '本地规则的适用条件尚未确定'})
    if decision_type is not None:
        result["reviewPlan"] = resolve_effective_review_plan(station, decision_type, chapter=design.get("chapter"))
    if binding() != initial_provider or (uses_roundtable and roundtable_resource() != private_roundtable):
        raise ProviderError('PROVIDER_CHANGED', 'local inputs changed while preparing; prepare again')
    result["planHash"] = "0" * 64
    while True:
        size = len(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        if result["characterCounts"]["response"] == size:
            break
        result["characterCounts"]["response"] = size
    result["planHash"] = sha256_json({key: value for key, value in result.items() if key != "planHash"})
    return result
