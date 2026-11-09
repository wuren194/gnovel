"""Current source write-set assembly and consumed manuscript evidence bindings."""
from __future__ import annotations

from pathlib import Path

from .project import TOOLBOX_ROOT
from .structure_store import StructureError, load_structure
from .write_sets import digest, file_state, json_bytes, safe_project_path


def renderer_fingerprint() -> str:
    paths = ['scripts/core/derived_views.py', 'scripts/core/settings_schema.py',
             'scripts/core/generated.py', 'scripts/core/entity_registry.py', 'scripts/core/write_sets.py',
             'scripts/core/jsonio.py', 'scripts/core/outline_facts.py', 'scripts/core/outline_constraints.py', 'shared/registries/workflow.json',
             'scripts/state_cache.py', 'shared/registries/settings.json', 'toolbox.manifest.json']
    paths.extend(['scripts/core/source_model.py', 'scripts/core/source_validation.py',
                  'scripts/core/source_impact.py', 'scripts/core/source_evidence.py',
                  'scripts/core/source_review.py', 'scripts/core/source_plan.py',
                  'scripts/core/pair_classification.py', 'scripts/core/prompt_conditions.py',
                  'scripts/core/source_rules.py', 'scripts/core/source_rule_issues.py', 'scripts/core/source_time.py',
                  'scripts/core/source_time_constraints.py',
                  'shared/registries/source-model.json'])
    return digest(json_bytes({p: digest((TOOLBOX_ROOT / p).read_bytes()) for p in paths}))



def source_files(root: Path) -> dict:
    """Only registered manuscript evidence contributes external source files."""
    from .source_evidence import EVIDENCE_PATH, read_evidence
    load_structure(root)
    evidence = read_evidence(root)
    sources = {EVIDENCE_PATH: file_state(safe_project_path(root, EVIDENCE_PATH))}
    for batch in evidence['batches'].values():
        sources[batch['path']] = file_state(safe_project_path(root, batch['path']))
    return sources


def derived_outputs(root: Path, structure: dict, *, extras: dict[str, bytes | None] | None = None,
                    current: dict | None = None) -> tuple[dict[str, bytes | None], dict]:
    """Assemble source and explicit domain outputs without maintaining Markdown copies."""
    outputs = {'.gnovel/structure.json': json_bytes(structure)}
    for path, data in (extras or {}).items():
        if path in outputs:
            raise StructureError('WRITE_TARGET_CONFLICT', 'multiple producers for one target', {'path': path})
        outputs[path] = data
    return outputs, {}
