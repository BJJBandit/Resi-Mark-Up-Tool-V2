"""The design process as a fixed dependency graph.

Order is decided by what each engine needs, not by an LLM:

    wind class ─┬─► racking / bracing (per storey, top storey first)
                ├─► tie-down (per storey)
                └─► roof members (rafters / verandah beams, if any)
    site class ──► footing system ──► piers / pads (needs loads from the load path)
    geometry ────► lintels (per storey) ──► steel members (if a lintel/beam is outside the timber tables)
                 ► floor members (per suspended floor) ─► columns ─► piers
    masonry ─────► articulation joints (per masonry storey)

Each step is scoped to the site, a building, or a storey, and carries the reason it
applies (or why it was skipped) so the process map on the logic page can show the
job's actual path. Steps are run in the order returned; a step whose dependency was
blocked still runs where it can, and inherits a flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from ..model import Building, Project, Storey

PREFS_PATH = Path(__file__).resolve().parents[1] / "data" / "house" / "preferences.yaml"

MASONRY = {"brick_veneer", "double_brick", "blockwork", "blockwork_corefilled"}
SUSPENDED = {"suspended_timber", "suspended_timber_on_steel", "suspended_slab", "bondek_slab"}


@dataclass
class PlanStep:
    id: str
    engine: str
    title: str
    scope: str  # "site" | building id | "building/storey"
    needs: list[str] = field(default_factory=list)
    reason: str = ""
    skipped: str | None = None  # reason when not applicable
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class DesignPlan:
    steps: list[PlanStep]
    sheets: dict[str, list[str]]  # building id -> sheet kinds (from preferences)
    background: list[str] = field(default_factory=list)  # computed but not issued (load paths)

    def active(self) -> list[PlanStep]:
        return [s for s in self.steps if s.skipped is None]

    def mermaid(self) -> str:
        """Process map of this job: taken steps solid, skipped steps dotted/grey."""
        lines = ["flowchart TD"]
        for s in self.steps:
            label = s.title.replace('"', "'")
            if s.skipped:
                lines.append(f'  {s.id}["{label}<br/><i>skipped: {s.skipped}</i>"]:::skipped')
            else:
                lines.append(f'  {s.id}["{label}<br/><small>{s.scope}</small>"]')
        for s in self.steps:
            for n in s.needs:
                arrow = "-.->" if s.skipped else "-->"
                lines.append(f"  {n} {arrow} {s.id}")
        lines.append("  classDef skipped fill:#eee,stroke:#aaa,color:#888,stroke-dasharray: 4 3;")
        return "\n".join(lines)


def load_prefs(path: Path = PREFS_PATH) -> dict:
    return yaml.safe_load(path.read_text())


# ---------------------------------------------------------------------------
# Applicability rules - each returns None (applies) or a reason to skip
# ---------------------------------------------------------------------------


def _has_masonry(b: Building) -> bool:
    return b.external_wall_construction in MASONRY or any(
        w.construction in MASONRY for s in b.storeys for w in s.walls
    )


def _rafter_roof(b: Building) -> bool:
    return b.roof.construction in {"rafter", "mixed"}


def _has_decks(b: Building) -> bool:
    return bool(b.decks)


def _suspended(s: Storey) -> bool:
    return s.floor.kind in SUSPENDED


def _timber_frame(b: Building) -> bool:
    return b.external_wall_construction in {"timber_stud", "brick_veneer"} or any(
        w.construction == "timber_stud" for s in b.storeys for w in s.walls
    )


def _sid(b: Building, s: Storey) -> str:
    return f"{b.id}/{s.id}"


def _storeys_top_down(b: Building) -> list[Storey]:
    order = {"second": 0, "first": 1, "ground": 2, "subfloor": 3}
    return sorted(b.storeys, key=lambda s: order.get(s.name, 9))


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def plan(project: Project, prefs: dict | None = None) -> DesignPlan:
    prefs = prefs or load_prefs()
    steps: list[PlanStep] = []

    def add(step: PlanStep, skip: Callable[[], str | None] | None = None) -> PlanStep:
        if skip is not None:
            step.skipped = skip()
        steps.append(step)
        return step

    # --- site-level -----------------------------------------------------
    wind = add(PlanStep("wind", "wind_as4055", "Wind classification (AS 4055)", "site",
                        reason="every bracing, tie-down and roof-member table is entered by wind class"))
    site = add(PlanStep("site", "site_as2870", "Site classification check (AS 2870 §2)", "site",
                        reason="site class drives footing system and masonry articulation"))

    for b in project.buildings:
        bid = b.id
        masonry = _has_masonry(b)
        storeys = _storeys_top_down(b)

        # --- footings ------------------------------------------------------
        foot = add(PlanStep(f"foot_{bid}", "footings_as2870", "Footing system (AS 2870 §3)", bid, needs=["site"],
                            reason=f"floor: {b.storeys[-1].floor.kind if b.storeys else 'unknown'}; walls: {b.external_wall_construction}"))

        # --- per storey, top down so lower storeys see the loads above ------
        prev_rack: str | None = None
        for s in storeys:
            sid = _sid(b, s)
            tag = f"{bid}_{s.id}"
            is_top = s is storeys[0]
            rack = add(PlanStep(f"rack_{tag}", "racking_as1684", f"Racking force & bracing (AS 1684.2 §8) – {s.name}", sid,
                                needs=["wind"] + ([prev_rack] if prev_rack else []),
                                reason="upper storey" if is_top else "lower storey of two – includes upper storey wind",
                                params={"storey_position": "single_or_upper" if is_top else "lower"}),
                       skip=lambda: None if _timber_frame(b) else "no timber-framed walls (bracing by masonry/steel – engineer)")
            prev_rack = rack.id

            add(PlanStep(f"lint_{tag}", "lintels_as1684", f"Lintels over openings – {s.name}", sid, needs=["wind"],
                         reason=f"{sum(len(w.openings) for w in s.walls)} openings in load-bearing walls",
                         params={"storey_position": "single_or_upper" if is_top else "lower"}),
                skip=lambda: None if any(w.openings for w in s.walls) else "no openings found")

            add(PlanStep(f"floor_{tag}", "floor_members_as1684", f"Bearers & joists – {s.name}", sid, needs=[],
                         reason=f"suspended floor ({s.floor.kind})"),
                skip=lambda: None if _suspended(s) else f"floor is {s.floor.kind}")

            add(PlanStep(f"tie_{tag}", "tie_down_as1684", f"Tie-down (AS 1684.2 §9) – {s.name}", sid, needs=["wind", rack.id],
                         reason="required for every framed storey"),
                skip=lambda: None if _timber_frame(b) else "no timber-framed walls")

            add(PlanStep(f"artic_{tag}", "articulation_as4773", f"Masonry articulation joints – {s.name}", sid, needs=["site"],
                         reason="masonry walls on a reactive site" if masonry else ""),
                skip=lambda: None if masonry else "no masonry")

        # --- roof & decks ----------------------------------------------------
        add(PlanStep(f"roof_{bid}", "roof_members_as1684", "Rafters / ridge / underpurlins", bid, needs=["wind"],
                     reason=f"roof construction: {b.roof.construction}"),
            skip=lambda: None if _rafter_roof(b) else "trussed roof – trusses by manufacturer")
        add(PlanStep(f"deck_{bid}", "deck_members_as1684", "Deck / verandah framing", bid, needs=["wind"],
                     reason=f"{len(b.decks)} deck(s)"),
            skip=lambda: None if _has_decks(b) else "no decks")

        # --- things that need the member results -----------------------------
        member_steps = [s.id for s in steps if s.scope.startswith(bid) and s.engine in
                        {"lintels_as1684", "floor_members_as1684", "roof_members_as1684", "deck_members_as1684"} and not s.skipped]
        add(PlanStep(f"steel_{bid}", "steel_as4100", "Steel beams & columns (AS 4100)", bid, needs=member_steps,
                     reason="any member outside the timber tables, and SHS columns"))
        add(PlanStep(f"loads_{bid}", "load_path", "Load path: roof → walls → beams → columns → footings", bid,
                     needs=member_steps + [foot.id],
                     reason="kept in the trace; not issued as a sheet"))
        add(PlanStep(f"pier_{bid}", "piers_pads", "Piers / pads under point loads", bid, needs=[f"loads_{bid}", foot.id],
                     reason="column and girder reactions to footings"))

    # --- sheets per building, from preferences --------------------------------
    sheet_prefs = prefs.get("sheets", {})
    sheets: dict[str, list[str]] = {}
    for b in project.buildings:
        if b.ncc_class.startswith("10"):
            sheets[b.id] = list(sheet_prefs.get("outbuilding", ["footing_slab", "roof_wall"]))
        elif b.storey_count >= 2:
            sheets[b.id] = list(sheet_prefs.get("two_storey", ["footing_slab", "wall_gf", "floor_ff", "wall_ff", "roof"]))
        else:
            sheets[b.id] = list(sheet_prefs.get("single_storey", ["footing_floor", "wall", "roof"]))

    background = ["load_path"] if not sheet_prefs.get("include_load_path_sheet", False) else []
    return DesignPlan(steps=_toposort(steps), sheets=sheets, background=background)


def _toposort(steps: list[PlanStep]) -> list[PlanStep]:
    """Stable topological order: keeps the authored order where dependencies allow."""
    by_id = {s.id: s for s in steps}
    done: list[PlanStep] = []
    seen: set[str] = set()

    def visit(s: PlanStep, stack: tuple[str, ...] = ()) -> None:
        if s.id in seen:
            return
        if s.id in stack:
            raise ValueError(f"cycle in design plan at {s.id}")
        for n in s.needs:
            if n in by_id:
                visit(by_id[n], stack + (s.id,))
        seen.add(s.id)
        done.append(s)

    for s in steps:
        visit(s)
    return done
