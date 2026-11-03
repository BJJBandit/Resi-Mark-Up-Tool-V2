"""House preferences (``app/data/house/preferences.yaml``) and the one rule that governs
how they meet a standard: **a preference may be more conservative than the standard, never
less.**

:func:`conservative_merge` takes the practice's proposal and the verified standard-table
row and returns the merged design plus notes for the trace wherever the table won.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

PREFS_PATH = Path(__file__).resolve().parents[1] / "data" / "house" / "preferences.yaml"


def load_preferences(path: Path | str = PREFS_PATH) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text()) or {}


def pref(prefs: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """``pref(prefs, "footings.strip_footing.depth_min")`` → 450."""
    cur: Any = prefs
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().split()[0])
        except (ValueError, IndexError):
            return None
    return None


def conservative_merge(
    proposal: dict[str, Any],
    verified: dict[str, Any],
    bigger_is_safer: Iterable[str],
    smaller_is_safer: Iterable[str] = (),
    citation: str = "the standard",
) -> tuple[dict[str, Any], list[str]]:
    """Merge the practice proposal with a verified table row.

    For each key in ``bigger_is_safer`` (depths, widths, bar counts …) the larger value wins;
    for ``smaller_is_safer`` (spacings, cover tolerances read as maxima) the smaller wins.
    Keys the table supplies that the proposal lacks are copied across. Where the table
    overrides the proposal a note is produced for the trace's ``conservatism_note``.
    Non-numeric keys present in both are left as the proposal with a note asking the engineer
    to compare (reinforcement callouts can't be compared automatically).
    """
    merged = dict(proposal)
    notes: list[str] = []
    for key, table_val in verified.items():
        if key not in merged:
            merged[key] = table_val
            continue
        p, t = _num(merged[key]), _num(table_val)
        if p is None or t is None:
            if str(merged[key]) != str(table_val):
                notes.append(
                    f"{key}: {citation} gives {table_val!r}, practice detail is {merged[key]!r} – engineer to confirm the practice detail is not less than the table."
                )
            continue
        if key in bigger_is_safer and t > p:
            notes.append(f"{key}: {citation} requires {table_val}, practice standard {merged[key]} – table adopted.")
            merged[key] = table_val
        elif key in smaller_is_safer and t < p:
            notes.append(f"{key}: {citation} limits to {table_val}, practice standard {merged[key]} – table adopted.")
            merged[key] = table_val
    return merged, notes
