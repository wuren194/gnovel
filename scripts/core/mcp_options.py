"""Shared MCP process options for server startup and static registration checks."""
from __future__ import annotations

import argparse


def build_parser(*, description=None, parser_class=argparse.ArgumentParser):
    parser = parser_class(description=description)
    parser.add_argument('--audience', help='工位受众，例如 writer；不改变兼容工具的可调用性')
    parser.add_argument('--task', help='当前阶段，例如 repair')
    parser.add_argument('--discovery-view', choices=('model', 'audit'), default='model')
    return parser


def startup_arguments_valid(arguments: list[str]) -> bool:
    class RegistrationParser(argparse.ArgumentParser):
        def exit(self, status=0, message=None):
            # --help is a valid CLI request, but cannot start a serving process.
            raise ValueError('server arguments exit before serving')

        def error(self, message):
            raise ValueError('invalid server arguments')

        def print_help(self, file=None):
            pass

    try:
        parsed = build_parser(parser_class=RegistrationParser).parse_args(arguments)
        if parsed.audience is not None or parsed.task is not None:
            # Reuse the actual discovery validator; a syntactically valid
            # filter must not make tools/list fail after startup.
            from .tool_catalog import list_capabilities
            list_capabilities(audience=parsed.audience, task=parsed.task, view=parsed.discovery_view)
    except (ValueError, TypeError):
        return False
    return True
