"""Deterministic design engines.

Each engine is a pure function ``run(project, building_id, tables, prefs, trace) -> Result``
whose decision tree lives as YAML in ``app/logic/`` and whose numeric data comes only from
the standards tables under ``app/data/standards/``. See ``app/engines/README.md``.
"""

from __future__ import annotations

from types import ModuleType

from . import footings, wind

#: engine id → module. Add a new engine here so the logic page and tests pick it up.
ENGINES: dict[str, ModuleType] = {
    wind.ENGINE_ID: wind,
    footings.ENGINE_ID: footings,
}

__all__ = ["ENGINES", "footings", "wind"]
