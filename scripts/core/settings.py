"""Read-only field projections and validation over current source truth."""
from __future__ import annotations

from pathlib import Path

from .entity_registry import PREFIX_BY_TYPE
from .settings_schema import settings_schema as _settings_schema
from .structure_store import load_structure, validate_structure


def settings_schema() -> dict:
    """Field presentation metadata; project values remain typed source records."""
    return _settings_schema(entity_types=set(PREFIX_BY_TYPE))


def query_settings(root: Path, *, selector: str | None = None, kind: str | None = None,
                   fields: list[str] | None = None, structure: dict | None = None) -> dict:
    """Select entities and project their registered source fields without reading exports."""
    from .consistency import require_no_pending
    from .source_fields import query_settings_source
    schema = settings_schema()
    if kind is not None and kind not in schema['kinds']:
        raise ValueError(f'unknown setting kind: {kind}')
    structure = load_structure(root) if structure is None else structure
    validate_structure(structure)
    require_no_pending(root)
    return query_settings_source(root, structure, selector=selector, kind=kind, fields=fields)


def check_settings(root: Path, *, structure: dict | None = None) -> dict:
    root = root.expanduser().resolve()
    structure = load_structure(root) if structure is None else structure
    validate_structure(structure)
    return {'schemaVersion': structure['schemaVersion'], 'valid': True, 'ready': True,
            'sources': ['.govel/structure.json'], 'errors': [], 'warnings': [],
            'coverage': {'structuralBindingsValidated': True, 'semanticReviewComplete': False}}
