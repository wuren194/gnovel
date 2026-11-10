"""Render executable CLI examples from the same parameter examples used by MCP."""
import json
import shlex

from .catalog_api import snake_case


def command_example(spec, arguments):
    parts = ['python3', 'scripts/novelctl.py', 'project', *spec.cli]
    for key, value in arguments.items():
        field = spec.input_schema['properties'][key]
        flag = '--' + snake_case(key).replace('_', '-')
        if key in spec.cli_positionals:
            parts.append(str(value))
        elif field.get('type') == 'boolean':
            if value:
                parts.append(flag)
        elif field.get('type') == 'array' and field.get('items', {}).get('type') == 'string':
            for item in value:
                parts.extend([flag, item])
        else:
            encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
            parts.extend([flag, encoded])
    return shlex.join(parts)
