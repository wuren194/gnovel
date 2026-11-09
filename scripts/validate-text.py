#!/usr/bin/env python3
"""Compatibility entrypoint for the modular text validator.

The implementation lives in ``scripts.text.validator``. Public and legacy helper
names are re-exported so tests, debug tools, and external callers keep working.
"""

from __future__ import annotations

try:
    from scripts.text import validator as _validator
except ImportError:
    from text import validator as _validator

for _name, _value in vars(_validator).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


if __name__ == "__main__":
    _validator.main()
