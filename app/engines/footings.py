"""Footing-system selection engine – AS 2870:2011.

The engineering logic, in plain English:

* The **site class** (geotech) decides whether the standard designs of Section 3 apply at
  all. Class P (problem sites: fill, soft ground, landslip, mine subsidence …) does not get a
  standard design – the engine proposes the practice's standard "strip footing on Ø450 bored
  piers to rock" and flags the job for engineering design (Section 4).
* The **AS 2870 construction type** (clad frame / articulated masonry veneer / masonry
  veneer / articulated full masonry / full masonry) comes from the external wall construction;
  stiffer, more crack-sensitive walls need stiffer footings, so it is a column of every
  standard design table.
* The **floor construction** picks the system: slab on ground → stiffened raft (practice
  standard; footing slab possible on A/S/M; waffle rafts are not used); suspended floor →
  strip footings under the load-bearing walls, on bored piers unless rock is within the
  strip footing depth.
* The **standard design table** (3.1 raft, 3.3 footing slab, 3.4 strip) is then looked up
  for the site class × construction type. While those tables are not loaded the lookup step
  is ``blocked`` with the exact citation and the practice proposal is carried, marked
  ``preference_applied``, so the UI can show "proposed per practice standard – AS 2870
  Table 3.4 not yet loaded to verify". Once loaded, the larger of table and practice wins
  for every depth / width (``conservatism_note`` records when the table overrode the
  practice).

Branching lives in ``app/logic/footings_as2870.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..model.schema import Building, Project, Storey
from .logic import LOGIC_DIR, LogicTree, Walk, load_tree, walk
from .prefs import conservative_merge, pref
from .tables import TableSet
from .trace import Status, Trace

ENGINE_ID = "footings_as2870"
LOGIC_FILE: Path = LOGIC_DIR / "footings_as2870.yaml"

# --- AS 2870:2011 cl 6.4.2 controlled fill limits ---------------------------------------
# TODO(standard: AS 2870:2011 cl 6.4.2) confirm the maximum depth of controlled fill under a
# slab for sand and for non-sand (clay) fill. The practice's slab-on-fill detail quotes two
# sets of limits – the engineer decides which applies. Blocked while None and fill present.
MAX_CONTROLLED_FILL_SAND_MM: float | None = None
MAX_CONTROLLED_FILL_OTHER_MM: float | None = None

#: Wall construction (schema) → AS 2870 construction type. Articulated variants are chosen
#: because the practice's standard notes require masonry to be articulated to AS 4773 /
#: AS 2870 App C; the engine flags that assumption (see ``construction_type``).
_CONSTRUCTION_TYPE = {
    "timber_stud": "clad_frame",
    "steel_frame": "clad_frame",
    "other": "clad_frame",
    "brick_veneer": "articulated_masonry_veneer",
    "double_brick": "articulated_full_masonry",
    "blockwork": "articulated_full_masonry",
    "blockwork_corefilled": "articulated_full_masonry",
    "concrete": "articulated_full_masonry",
}
_UNARTICULATED = {
    "articulated_masonry_veneer": "masonry_veneer",
    "articulated_full_masonry": "full_masonry",
}

# Keys of a proposal / table row where a larger value is the safer one.
_BIGGER_IS_SAFER = (
    "depth_mm", "width_mm",
    "edge_beam_depth_mm", "edge_beam_width_mm",
    "edge_footing_depth_mm", "edge_footing_width_mm",
    "internal_beam_depth_mm", "internal_beam_width_mm",
    "slab_thickness_mm",
)
_SMALLER_IS_SAFER = ("max_beam_spacing_mm",)


class FootingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str = ENGINE_ID
    building_id: str
    site_class: str
    construction_type: str | None = None
    floor_kind: str | None = None
    footing_system: str | None = Field(None, description="stiffened_raft | footing_slab | strip_and_piers | strip_on_rock | piers_to_rock")
    design_basis: str = Field("standard", description="standard (Section 3) | engineered (Section 4)")
    proposal: dict[str, Any] = Field(default_factory=dict, description="Practice proposal, merged with the table where loaded.")
    verified: bool = Field(False, description="True when the standard design table was loaded and consulted.")
    table_row: dict[str, Any] | None = None
    status: Status = "ok"
    flags: list[str] = Field(default_factory=list)
    blocked_on: list[str] = Field(default_factory=list)
    conservatism_notes: list[str] = Field(default_factory=list)
    visited: list[str] = Field(default_factory=list)


def tree() -> LogicTree:
    return load_tree(LOGIC_FILE)


# ---------------------------------------------------------------------------
# Derived inputs
# ---------------------------------------------------------------------------


def _ground_storey(b: Building) -> Storey | None:
    for s in b.storeys:
        if s.name == "ground":
            return s
    return b.storeys[0] if b.storeys else None


def construction_type(b: Building, trace: Trace) -> str:
    """Map the external wall construction to the AS 2870 construction type and record it."""
    ct = _CONSTRUCTION_TYPE.get(b.external_wall_construction, "clad_frame")
    inputs = {"external_wall_construction": b.external_wall_construction}
    if ct in _UNARTICULATED:
        trace.step(
            ENGINE_ID, "AS 2870 construction type", inputs=inputs, result={"construction_type": ct},
            citation="AS 2870:2011 cl 1.3 / Table 1.1 / App C", status="flag", preference_applied="notes.articulation",
            rule=(
                f"Masonry taken as *{ct.replace('_', ' ')}* because the practice's standard notes require masonry to be "
                f"articulated to AS 4773 / AS 2870 App C. If the drawings do not show articulation joints the footing must be "
                f"selected for *{_UNARTICULATED[ct].replace('_', ' ')}* (deeper). The model has no articulation field yet – confirm."
            ),
        )
    else:
        trace.step(
            ENGINE_ID, "AS 2870 construction type", inputs=inputs, result={"construction_type": ct},
            citation="AS 2870:2011 cl 1.3 / Table 1.1",
            rule="Framed walls with lightweight cladding are 'clad frame' – the least footing-sensitive construction type.",
        )
    return ct


def fill_check(project: Project, trace: Trace) -> None:
    g = project.site.geotech
    if not g.fill_present:
        trace.step(
            ENGINE_ID, "Controlled fill", inputs={"fill_present": g.fill_present}, citation="AS 2870:2011 cl 6.4.2",
            rule="No fill reported under the footings; cl 6.4.2 depth limits do not arise.",
        )
        return
    limits = {"sand": MAX_CONTROLLED_FILL_SAND_MM, "other": MAX_CONTROLLED_FILL_OTHER_MM}
    if any(v is None for v in limits.values()):
        trace.step(
            ENGINE_ID, "Controlled fill", inputs={"fill_present": True, "fill_depth_mm": g.fill_depth_mm},
            citation="AS 2870:2011 cl 6.4.2", status="blocked",
            rule="Fill is present but the cl 6.4.2 maximum controlled-fill depths are not yet entered in footings.py – TODO(standard: AS 2870:2011 cl 6.4.2). Engineer to check fill depth and compaction (AS 3798).",
        )
        return
    limit = MAX_CONTROLLED_FILL_OTHER_MM  # conservative: unless the fill is known to be sand
    status: Status = "ok" if (g.fill_depth_mm is not None and g.fill_depth_mm <= limit) else "flag"  # type: ignore[operator]
    trace.step(
        ENGINE_ID, "Controlled fill", inputs={"fill_depth_mm": g.fill_depth_mm, "limit_mm": limit},
        citation="AS 2870:2011 cl 6.4.2", status=status,
        rule="Fill depth compared with the non-sand limit (conservative). Over the limit, or depth unknown, the footings must found below the fill or be designed for it.",
    )


# ---------------------------------------------------------------------------
# Practice proposals
# ---------------------------------------------------------------------------


def _pier(prefs: dict[str, Any]) -> dict[str, Any]:
    p = pref(prefs, "footings.pier", {}) or {}
    return {
        "tag": "P2",
        "diameter_mm": p.get("diameter"),
        "reinforcement": p.get("reinforcement"),
        "founding": p.get("founding"),
        "at_corners": p.get("at_corners"),
        "max_spacing_mm": p.get("max_spacing_under_strip"),
    }


def _strip(prefs: dict[str, Any], class_p: bool = False) -> dict[str, Any]:
    s = pref(prefs, "footings.strip_footing", {}) or {}
    return {
        "tag": "SF1",
        "width_mm": s.get("width"),
        "depth_mm": s.get("depth_min_class_P") if class_p else s.get("depth_min"),
        "reinforcement": s.get("reinforcement"),
    }


def _slab(prefs: dict[str, Any], garage: bool = False) -> dict[str, Any]:
    s = pref(prefs, "footings.slab_garage" if garage else "footings.slab_house", {}) or {}
    return {"slab_thickness_mm": s.get("thickness"), "slab_mesh": s.get("mesh"), "concrete": s.get("concrete")}


def _raft(prefs: dict[str, Any], b: Building) -> dict[str, Any]:
    eb = pref(prefs, "footings.edge_beam", {}) or {}
    ib = pref(prefs, "footings.internal_beam", {}) or {}
    out = {
        "edge_beam_tag": "EB1",
        "edge_beam_width_mm": eb.get("width"),
        "edge_beam_depth_mm": eb.get("depth_min"),
        "edge_beam_reinforcement": eb.get("reinforcement"),
        "internal_beam_tag": "IB1",
        "internal_beam_width_mm": ib.get("width"),
        "internal_beam_depth_mm": ib.get("depth_min"),
        "internal_beam_reinforcement": ib.get("reinforcement"),
    }
    out.update(_slab(prefs, garage=b.ncc_class.startswith("10")))
    return out


def proposal_for(key: str, prefs: dict[str, Any], b: Building) -> tuple[dict[str, Any], str]:
    """(proposal dict, preferences.yaml key it came from) for a tree ``proposal_key``."""
    if key == "strip_and_piers":
        return {**_strip(prefs), "piers": _pier(prefs)}, "footings.strip_footing + footings.pier"
    if key == "strip_and_piers_class_p":
        return {**_strip(prefs, class_p=True), "piers": _pier(prefs)}, "footings.strip_footing.depth_min_class_P + footings.pier"
    if key == "strip_on_rock":
        s = _strip(prefs)
        s["on_rock_note"] = pref(prefs, "footings.strip_footing.reduce_on_rock")
        return s, "footings.strip_footing"
    if key in ("stiffened_raft", "footing_slab"):
        return _raft(prefs, b), "footings.edge_beam + footings.internal_beam + footings.slab_house"
    if key == "slab_on_piers_class_p":
        r = _raft(prefs, b)
        r["piers"] = {**_pier(prefs), "tag": "P3", "note": "600 taper at top under slab (practice P3)"}
        return r, "footings.edge_beam + footings.internal_beam + footings.pier"
    return {}, ""


# ---------------------------------------------------------------------------
# Load path
# ---------------------------------------------------------------------------


def load_path(b: Building, system: str | None, trace: Trace) -> None:
    """Record how gravity load reaches the ground. Kept in the trace (practice decision:
    no Load Path sheet)."""
    storeys = [s for s in b.storeys if s.name != "subfloor"]
    storeys.sort(key=lambda s: ["ground", "first", "second"].index(s.name) if s.name in ("ground", "first", "second") else 0)
    upper = storeys[1:]
    trace.load_path(ENGINE_ID, "roof", f"{storeys[-1].name if storeys else 'ground'}-storey external walls",
                    rule="Roof trusses / rafters bear on the external (and any load-bearing internal) walls.", citation="AS 1684.2:2021 Section 2")
    for s in reversed(upper):
        below = storeys[storeys.index(s) - 1]
        trace.load_path(ENGINE_ID, f"{s.name}-storey walls", f"{s.name} floor framing", rule="Wall loads and floor loads collect in bearers / floor beams.")
        trace.load_path(ENGINE_ID, f"{s.name} floor framing", f"{below.name}-storey walls / columns", rule="Bearers bear on the walls and columns of the storey below.")
    ground = "ground-storey external walls"
    if system in ("strip_and_piers", "strip_and_piers_class_p"):
        trace.load_path(ENGINE_ID, ground, "strip footing SF1", rule="Load-bearing walls on foundation brickwork / bearers to the strip footing.")
        trace.load_path(ENGINE_ID, "strip footing SF1", "bored piers P2", rule="Strip footing spans between piers at corners and max 3000 ctrs.")
        trace.load_path(ENGINE_ID, "bored piers P2", "rock / refusal", rule="Piers carry the load to rock or refusal (practice founding rule).")
    elif system == "strip_on_rock":
        trace.load_path(ENGINE_ID, ground, "strip footing SF1", rule="Load-bearing walls to the strip footing.")
        trace.load_path(ENGINE_ID, "strip footing SF1", "rock", rule="Strip footing founded directly on rock; piers omitted.")
    elif system in ("stiffened_raft", "footing_slab"):
        trace.load_path(ENGINE_ID, ground, "edge beam EB1", rule="External walls on the slab edge beam; internal load-bearing walls on internal beams IB1.")
        trace.load_path(ENGINE_ID, "edge beam EB1", "founding soil", rule="Raft bears on natural ground / controlled fill at the founding depth.")
    elif system == "piers_to_rock":
        trace.load_path(ENGINE_ID, ground, "footing beams on piers", rule="Walls on strip footings / edge beams spanning between piers.")
        trace.load_path(ENGINE_ID, "footing beams on piers", "bored piers to rock", rule="Class P – all load taken to rock / refusal by piers (engineered).")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def run(project: Project, building_id: str, tables: TableSet, prefs: dict[str, Any], trace: Trace) -> FootingResult:
    b = project.building(building_id)
    g = project.site.geotech
    t = tree()
    storey = _ground_storey(b)
    floor_kind = storey.floor.kind if storey else "unknown"

    with trace.section(ENGINE_ID, t.title, citation=t.standard) as section:
        ct = construction_type(b, trace)
        fill_check(project, trace)

        strip_depth = pref(prefs, "footings.strip_footing.depth_min")
        founds_on_rock = bool(g.rock_depth_mm is not None and strip_depth is not None and g.rock_depth_mm <= strip_depth)
        systems_in_use = pref(prefs, "materials.footing_systems_in_use", []) or []
        slab_system = "stiffened_raft" if "stiffened_raft" in systems_in_use else ("footing_slab" if "footing_slab" in systems_in_use else "stiffened_raft")

        variables: dict[str, Any] = {
            "site_class": g.site_class,
            "construction_type": ct,
            "floor_kind": floor_kind,
            "slab_system": slab_system,
            "strip_founds_on_rock": founds_on_rock,
            "rock_depth_mm": g.rock_depth_mm,
            "trees_or_abnormal_moisture": g.trees_or_abnormal_moisture,
            "bearing_kpa": g.bearing_kpa,
        }
        trace.step(
            ENGINE_ID, "Inputs read from site and building model", inputs=dict(variables), citation="AS 2870:2011 Section 2",
            rule=f"Site class from the geotech report; floor construction from the ground storey; 'strip founds on rock' = rock depth ≤ practice strip depth ({strip_depth} mm).",
            preference_applied="materials.footing_systems_in_use",
        )

        wk: Walk = walk(t, variables, tables, trace)

        system = wk.results.get("footing_system")
        key = wk.results.get("proposal_key")
        standard_design = wk.results.get("standard_design")
        lookup_steps = [s for s in wk.steps if s.kind == "lookup"]
        lookup_citation = lookup_steps[-1].citation if lookup_steps else ""
        verified = standard_design is not None
        notes: list[str] = []
        proposal: dict[str, Any] = {}

        if key:
            proposal, pref_key = proposal_for(str(key), prefs, b)
            if verified:
                proposal, notes = conservative_merge(proposal, standard_design, _BIGGER_IS_SAFER, _SMALLER_IS_SAFER, citation=lookup_citation)
                trace.step(
                    ENGINE_ID, "Practice standard checked against the standard design", inputs={"table": standard_design},
                    result=proposal, citation=lookup_citation, preference_applied=pref_key,
                    status="ok" if not notes else "flag",
                    conservatism_note=" ".join(notes) or None,
                    rule="For every depth / width the larger of the standard design and the practice standard is adopted; reinforcement callouts are listed for the engineer to compare.",
                )
            elif wk.results.get("design_basis") == "engineered":
                trace.step(
                    ENGINE_ID, "Practice standard proposed (engineered design)", result=proposal, preference_applied=pref_key,
                    status="flag", citation="AS 2870:2011 Section 4",
                    rule="Class P – proposal is the practice's standard pier-supported footing; it is a starting point for the engineer's design, not a verified standard design.",
                )
            else:
                trace.step(
                    ENGINE_ID, "Practice standard proposed – not yet verified", result=proposal, preference_applied=pref_key,
                    status="flag", citation=lookup_citation,
                    rule=f"Proposed per practice standard – {lookup_citation} not yet loaded to verify. Once the table is loaded the larger of table and practice governs.",
                )

        load_path(b, str(key) if key else system, trace)
        section.result = {"footing_system": system, "verified": verified}

    return FootingResult(
        building_id=building_id,
        site_class=g.site_class,
        construction_type=ct,
        floor_kind=floor_kind,
        footing_system=system,
        design_basis=str(wk.results.get("design_basis", "standard")),
        proposal=proposal,
        verified=verified,
        table_row=standard_design,
        status=section.rolled_status,
        flags=list(wk.flags),
        blocked_on=trace.blocked_on(),
        conservatism_notes=notes,
        visited=wk.visited,
    )
