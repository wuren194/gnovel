#!/usr/bin/env python3
"""CLI entrypoint for the independently installable govel-bind skill."""
from pathlib import Path
import runpy


if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'skills/govel-bind/scripts/bind.py'),
                  run_name='__main__')
