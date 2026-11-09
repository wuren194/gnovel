#!/usr/bin/env python3
"""Create and validate explicit local data directories."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.core.data_provider import ProviderError, active_provider, binding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='create an example provider in an empty external directory')
    create.add_argument('--root', type=Path, required=True)
    commands.add_parser('validate', help='validate the configured provider without printing its contents')
    args = parser.parse_args(argv)
    try:
        if args.command == 'create':
            from scripts.core.data_provider import ROOT
            target = args.root.expanduser().absolute()
            if (target.is_symlink() or target.resolve().is_relative_to(ROOT)
                    or target.exists() and (not target.is_dir() or any(target.iterdir()))):
                raise ProviderError('PROVIDER_DESTINATION_INVALID', 'use an empty directory outside the checkout')
            sample = json.loads((ROOT / 'knowledge/example.json').read_text())
            body = '---\n' + json.dumps(sample['metadata'], ensure_ascii=False, indent=2) + '\n---\n\n' + sample['body'] + '\n'
            from scripts.core.knowledge_store import _parse
            _parse(body.encode(), 'example.md')
            target.mkdir(parents=True, exist_ok=True)
            assets = target / 'knowledge/assets'
            assets.mkdir(parents=True)
            (assets / 'example.md').write_text(body)
            manifest = {'schemaVersion': 1, 'providerId': 'local-example', 'version': '1', 'registries': {}, 'materials': []}
            (target / 'provider.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
            config = target / 'config.json'
            config.write_text(json.dumps({'schemaVersion': 1, 'root': str(target.resolve())}, ensure_ascii=False, indent=2) + '\n')
            result = {'ok': True, 'code': 'PROVIDER_CREATED', 'config': str(config)}
        else:
            provider = active_provider()
            from scripts.core.knowledge_store import list_assets
            result = {'ok': True, 'code': 'PROVIDER_VALID', 'configured': provider is not None,
                      'binding': binding(), 'assetCount': len(list_assets()['items'])}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'code': getattr(exc, 'code', 'PROVIDER_INVALID'), 'message': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
