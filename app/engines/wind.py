"""Wind classification engine – AS 4055:2021.

What it does, in the order the engineer would:

1. **Scope** (cl 1.2): is the building within the limits of application (height, width,
   roof pitch)? The limit values are *not* typed here from memory – the constants below are
   ``None`` until confirmed from the licensed standard, and the scope step is ``blocked``
   while they are.
2. **Inputs** (cl 2.2–2.5): region, terrain category, shielding and topographic class from
   ``Site.wind``. Topographic class may be derived from the site slope via Table 2.1.
3. **Classification**: Table 2.2 for Region A gives the N-class. Regions B–D have tables
   2.3–2.5 which the practice does not use – they are ``not_supported`` unless loaded.
4. **Check**: compare with the class stated by the architect / geotech and flag any
   difference. If the site inputs are missing but a class is stated, adopt it with a flag.

All branching is in ``app/logic/wind_as4055.yaml``; this module computes the derived inputs,
walks the tree and packages the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..model.schema import Building, Project
from .logic import LOGIC_DIR, LogicTree, Walk, load_tree, walk
from .tables import TableSet
from .trace import Status, Trace

ENGINE_ID = "wind_as4055"
LOGIC_FILE: Path = LOGIC_DIR / "wind_as4055.yaml"

# --- AS 4055:2021 cl 1.2 limits of application -----------------------------------------
# TODO(standard: AS 4055:2021 cl 1.2) confirm each value from the licensed copy, then the
# scope step unblocks. Do not fill from memory.
MAX_HEIGHT_MM: float | None = None  # max height from ground to the highest point of the roof (cl 1.2)
MAX_WIDTH_MM: float | None = None  # max building width, roofed verandahs included, eaves excluded (cl 1.2)
MAX_ROOF_PITCH_DEG: float | None = None  # max roof pitch (cl 1.2)

#: Wind classes in order of increasing severity – used to decide which of two classes is
#: the more conservative. Ordering only; no numeric values from the standard.
CLASS_ORDER = ["N1", "N2", "N3", "N4", "N5", "N6", "C1", "C2", "C3", "C4"]


class WindResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str = ENGINE_ID
    building_id: str
    wind_class: str | None = Field(None, description="Class to design to (None when blocked).")
    derived_class: str | None = Field(None, description="Class from the AS 4055 lookup, if made.")
    stated_class: str | None = None
    source: str = Field("", description="derived | stated | stated_provisional | ''")
    region: str = "A"
    terrain_category: str | None = None
    shielding: str | None = None
    topographic_class: str | None = None
    scope_check: str = "unverified"
    status: Status = "ok"
    flags: list[str] = Field(default_factory=list)
    blocked_on: list[str] = Field(default_factory=list)
    visited: list[str] = Field(default_factory=list, description="Logic-tree node ids, in order.")


def tree() -> LogicTree:
    return load_tree(LOGIC_FILE)


# ---------------------------------------------------------------------------
# Derived inputs
# ---------------------------------------------------------------------------


def _footprint_width_mm(b: Building) -> float | None:
    """Smaller plan dimension of the footprint bounding box (AS 4055 'width' is the
    narrower overall dimension). Falls back to the ground-storey external walls."""
    pts = list(b.footprint.points) if b.footprint else []
    if not pts:
        for s in b.storeys:
            for w in s.external_walls():
                pts += [w.start, w.end]
            if pts:
                break
    if len(pts) < 2:
        return None
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return min(max(xs) - min(xs), max(ys) - min(ys))


def _approx_height_mm(b: Building) -> float | None:
    """Lower-bound building height: sum of storey wall heights above the subfloor. Excludes
    floor zones and the roof rise, so a *pass* on this figure is not conclusive – the step
    says so. A proper height needs a schema field (see README – wanted fields)."""
    storeys = [s for s in b.storeys if s.name != "subfloor"]
    if not storeys:
        return None
    return float(sum(s.wall_height_mm for s in storeys))


def scope_check(b: Building, trace: Trace) -> str:
    """Record the cl 1.2 scope step and return pass | fail | unverified."""
    width = _footprint_width_mm(b)
    height = _approx_height_mm(b)
    pitch = b.roof.pitch_deg
    inputs = {"width_mm": width, "approx_height_mm": height, "roof_pitch_deg": pitch}
    limits = {"MAX_HEIGHT_MM": MAX_HEIGHT_MM, "MAX_WIDTH_MM": MAX_WIDTH_MM, "MAX_ROOF_PITCH_DEG": MAX_ROOF_PITCH_DEG}

    if any(v is None for v in limits.values()):
        trace.step(
            ENGINE_ID,
            "Scope check – limits of application",
            kind="scope",
            inputs=inputs,
            citation="AS 4055:2021 cl 1.2",
            status="blocked",
            rule=(
                "The cl 1.2 limit values (height, width, roof pitch) are not yet entered in wind.py – "
                "TODO(standard: AS 4055:2021 cl 1.2). Scope not checked; classification is provisional."
            ),
            result={"scope_check": "unverified", "limits": limits},
        )
        return "unverified"

    fails = []
    if height is not None and height > MAX_HEIGHT_MM:  # type: ignore[operator]
        fails.append(f"height {height:.0f} > {MAX_HEIGHT_MM}")
    if width is not None and width > MAX_WIDTH_MM:  # type: ignore[operator]
        fails.append(f"width {width:.0f} > {MAX_WIDTH_MM}")
    if pitch is not None and pitch > MAX_ROOF_PITCH_DEG:  # type: ignore[operator]
        fails.append(f"roof pitch {pitch} > {MAX_ROOF_PITCH_DEG}")
    unknown = [k for k, v in inputs.items() if v is None]

    if fails:
        trace.step(
            ENGINE_ID, "Scope check – limits of application", kind="scope", inputs=inputs,
            citation="AS 4055:2021 cl 1.2", status="blocked",
            rule="Building exceeds AS 4055 limits: " + "; ".join(fails) + ".", result={"scope_check": "fail"},
        )
        return "fail"
    if unknown:
        trace.step(
            ENGINE_ID, "Scope check – limits of application", kind="scope", inputs=inputs,
            citation="AS 4055:2021 cl 1.2", status="flag",
            rule=f"Geometry incomplete ({', '.join(unknown)}) – scope could not be fully checked. Height is approximate (wall heights only).",
            result={"scope_check": "unverified"},
        )
        return "unverified"
    trace.step(
        ENGINE_ID, "Scope check – limits of application", kind="scope", inputs=inputs,
        citation="AS 4055:2021 cl 1.2", status="ok",
        rule="Height, width and roof pitch are within the cl 1.2 limits (height approximate – wall heights only).",
        result={"scope_check": "pass"},
    )
    return "pass"


def _more_severe(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b, key=lambda c: CLASS_ORDER.index(c) if c in CLASS_ORDER else -1)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def run(project: Project, building_id: str, tables: TableSet, prefs: dict[str, Any], trace: Trace) -> WindResult:
    """Classify the wind for ``building_id``. Pure: reads the project, tables and prefs;
    writes only to ``trace``."""
    b = project.building(building_id)
    w = project.site.wind
    t = tree()

    with trace.section(ENGINE_ID, t.title, citation=t.standard) as section:
        scope = scope_check(b, trace)

        inputs_complete = bool(w.terrain_category and w.shielding and (w.topographic_class or w.site_slope_deg is not None))
        variables: dict[str, Any] = {
            "scope_check": scope,
            "region": w.region,
            "terrain_category": w.terrain_category,
            "shielding": w.shielding,
            "topographic_class": w.topographic_class,
            "site_slope_deg": w.site_slope_deg,
            "stated_class": w.stated_class,
            "stated_source": w.stated_source,
            "inputs_complete": inputs_complete,
        }
        trace.step(
            ENGINE_ID, "Inputs read from site model", inputs=dict(variables), citation="AS 4055:2021 cl 2.2–2.5",
            rule="Region from Fig 2.1 (practice default A – Tasmania); terrain, shielding and topography as entered by the engineer / geotech.",
        )

        wk: Walk = walk(t, variables, tables, trace)

        derived = wk.vars.get("wind_class") if wk.results.get("wind_class_source") in ("derived",) else None
        if "classification" in wk.results:
            derived = wk.results["classification"].get("wind_class")
        final = wk.vars.get("wind_class")
        source = str(wk.results.get("wind_class_source", ""))

        # Conservatism: if the stated class is more severe than the derived one, adopt the
        # stated class until the site inputs are confirmed – never relax a stated class on
        # unverified inputs.
        if "disagrees" in wk.visited and derived and w.stated_class:
            conservative = _more_severe(derived, w.stated_class)
            if conservative != derived:
                trace.step(
                    ENGINE_ID, "Conservative class adopted", inputs={"derived": derived, "stated": w.stated_class},
                    result={"wind_class": conservative}, status="flag", citation="AS 4055:2021 cl 2.2",
                    rule="The stated class is more severe than the derived one; the stated class is adopted pending confirmation of the terrain / shielding / topography inputs.",
                    conservatism_note=f"Stated {w.stated_class} adopted over derived {derived}.",
                )
                final = conservative
                source = "stated_conservative"

        section.result = {"wind_class": final, "source": source}
        section.status = wk.status

    return WindResult(
        building_id=building_id,
        wind_class=final,
        derived_class=derived,
        stated_class=w.stated_class,
        source=source,
        region=w.region,
        terrain_category=w.terrain_category,
        shielding=w.shielding,
        topographic_class=wk.vars.get("topographic_class"),
        scope_check=scope,
        status=section.rolled_status,
        flags=list(wk.flags) + ([] if scope != "unverified" else []),
        blocked_on=trace.blocked_on(),
        visited=wk.visited,
    )
