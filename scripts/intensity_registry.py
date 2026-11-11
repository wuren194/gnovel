#!/usr/bin/env python3
"""Loader for the intensity registry (shared/registries/intensity.json).

The registry is the single mechanical source of truth for level semantics and
lexicons.  Both intensity consumers read through this module so a word can
never drift between the ceiling check (ai_cliche_linter) and the floor check
(validate-text S-09b):

- ceiling: ``ceiling_violations(level)`` -> words forbidden at that level
- floor:   ``degradation_floor()``       -> S-09b word pool and thresholds
- gate:    ``extreme_level()`` / ``extreme_markers()`` -> Level 10 human gate

intensity.json 是全库唯一真源，本模块是唯一读取者（runtime 已移除）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REGISTRY_PATH = Path(__file__).resolve().parent.parent / "shared" / "registries" / "intensity.json"
SUPPORTED_SCHEMA_VERSION = 4


class IntensityRegistryError(RuntimeError):
    """The registry is missing, unreadable, or of an unsupported version."""


def load() -> dict[str, Any]:
    try:
        from scripts.core.data_provider import registry
    except ImportError:
        from core.data_provider import registry
    try:
        data = registry('intensity', REGISTRY_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        raise IntensityRegistryError(f"无法加载张力等级注册表 {REGISTRY_PATH}: {exc}") from exc
    if data.get("schemaVersion") != SUPPORTED_SCHEMA_VERSION:
        raise IntensityRegistryError(f"不支持的张力等级注册表版本: {data.get('schemaVersion')}")
    return data


def default_level() -> int:
    return int(load()["defaultLevel"])


def level_bounds() -> tuple[int, int]:
    registry = load()
    return int(registry["minLevel"]), int(registry["maxLevel"])


def extreme_level() -> int:
    return int(load()["extremeLevel"])


def ceiling_violations(level: int) -> tuple[str, ...]:
    """Words that must not appear when ``level`` is declared as the ceiling."""
    table = load()["ceilingViolations"]
    return tuple(table.get(str(level), ()))


def degradation_floor() -> dict[str, Any]:
    """S-09b configuration: word pool, size-tiered thresholds, L6 add-ons."""
    return load()["degradationFloor"]


def contrast_config() -> dict[str, Any]:
    """S-09c configuration: window size and both signal pools (elevation,
    grouped by dimension; degradation).  Pools are flattened for matching."""
    contrast = degradation_floor()["contrast"]
    elevation = contrast["elevationSignals"]
    if isinstance(elevation, dict):
        elevation = [word for group in elevation.values() for word in group]
    return {
        "windowSize": int(contrast["windowSize"]),
        "elevationSignals": tuple(elevation),
        "degradationSignals": tuple(contrast["degradationSignals"]),
    }


def floor_thresholds(text_size: int, intensity_level: int) -> dict[str, int]:
    """Resolve the S-09b requirement counts for this text size and level.

    Returns keys: words, smell, sensoryStay, sticky, pollutionChain, voiceLines,
    contrast.
    """
    config = degradation_floor()
    tier: dict[str, Any] | None = None
    for candidate in config["thresholds"]:
        limit = candidate.get("belowTextSize")
        if limit is None or text_size < int(limit):
            tier = candidate
            break
    if tier is None:  # pragma: no cover - the registry always ends with a null tier
        raise IntensityRegistryError("degradationFloor.thresholds 缺少兜底档（belowTextSize: null）")
    required = {
        "words": int(tier["words"]),
        "smell": int(tier["smell"]),
        "sensoryStay": int(tier["sensoryStay"]),
        "sticky": int(tier["sticky"]),
        "contrast": int(tier["contrast"]),
        "pollutionChain": 0,
        "voiceLines": 0,
    }
    if intensity_level < 6:
        return required
    level6 = config["level6"]
    large_floor = level6["largeTextFloor"]
    bonus = level6["smallTextBonus"]
    if text_size < 2000:
        for key, extra in bonus.items():
            required[key] += int(extra)
    else:
        for key, value in large_floor.items():
            required[key] = int(value)
    required["pollutionChain"] = int(level6["pollutionChain"])
    required["voiceLines"] = int(level6["voiceLines"])
    return required


def extreme_markers() -> tuple[str, ...]:
    return tuple(load()["extremeMarkers"]["words"])


def ambiguous_ceiling_words() -> tuple[str, ...]:
    return tuple(load()['ambiguousCeilingWords'])
