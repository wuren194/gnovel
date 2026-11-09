"""Entity type display metadata. Authored fields live in source fieldDefinitions."""
from collections.abc import Collection
from .project import TOOLBOX_ROOT, toolbox_manifest


def settings_schema(*, entity_types: Collection[str] | None = None) -> dict:
    from .static_cache import cached_json
    schema = cached_json(TOOLBOX_ROOT / toolbox_manifest()['sharedProtocols']['settingsSchema'])
    if schema.get('schemaVersion') != 1 or set(schema) != {'schemaVersion', 'kinds'}:
        raise ValueError('invalid entity presentation schema')
    kinds = schema['kinds']
    if not isinstance(kinds, dict) or not kinds or any(not isinstance(spec, dict) or set(spec) != {'title'}
            or not isinstance(spec['title'], str) or not spec['title'].strip() for spec in kinds.values()):
        raise ValueError('each entity kind requires its display title')
    if entity_types is not None and set(kinds) != set(entity_types):
        raise ValueError('entity presentation schema must cover the registered entity types')
    return schema
