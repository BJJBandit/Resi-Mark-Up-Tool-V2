"""Turn engine results into mark-up sheets (the `MarkupSheet` objects `app.pdf` draws).

Every element placed here can be traced: footing lines follow load-bearing walls from the
model, piers come from `layout.place_piers` with the spacing the footings engine proposed,
schedule text is built from the proposal dict, notes come from the notes library with
their conditions evaluated against the model. Nothing is free text invented here.

Sheets whose engines are not yet available (bracing, lintels, floor members) are still
produced with their banner, notes and column schedule, and a warning says what is missing,
so the set is complete and honest rather than silently partial.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
import yaml

from ..label.plan_image import mm_to_page_pt
from ..model import Building, Project, Pt, Storey, Wall
from ..schemas import Box, Element, MarkupSheet, Point, ScheduleItem
from .layout import place_piers
from .runner import DesignRun

NOTES_PATH = Path(__file__).resolve().parents[1] / "data" / "house" / "notes_library.yaml"

SHEET_TITLES_V2: dict[str, str] = {
    "footing_floor": "FOOTING AND FLOOR FRAMING PLAN",
    "footing_slab": "FOOTING AND SLAB PLAN",
    "wall": "WALL FRAMING PLAN",
    "wall_gf": "WALL FRAMING PLAN - GROUND FLOOR",
    "floor_ff": "FLOOR FRAMING PLAN - FIRST FLOOR",
    "wall_ff": "WALL FRAMING PLAN - FIRST FLOOR",
    "roof": "ROOF FRAMING PLAN",
    "roof_wall": "ROOF/WALL FRAMING PLAN",
}
# Which notes-library categories each sheet kind draws from.
SHEET_CATEGORIES: dict[str, set[str]] = {
    "footing_floor": {"footing", "floor"}, "footing_slab": {"footing"}, "wall": {"wall"}, "wall_gf": {"wall"},
    "floor_ff": {"floor"}, "wall_ff": {"wall"}, "roof": {"roof"}, "roof_wall": {"roof", "wall"},
}
FOOTING_SHEETS = {"footing_floor", "footing_slab"}
PLAN_MARGIN_MM = 1500


@dataclass
class SheetOut:
    building_id: str
    kind: str
    title: str
    sheet: MarkupSheet
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------


class Mapper:
    """Building mm → normalised page coordinates (0..1, top-left origin) for one storey."""

    def __init__(self, storey: Storey, page_size: tuple[float, float]) -> None:
        if storey.frame is None:
            raise ValueError(f"storey {storey.id} has no plan frame")
        self.frame = storey.frame
        self.w, self.h = page_size

    def pt(self, p: Pt) -> Point:
        q = mm_to_page_pt(self.frame, p, self.h)
        return Point(x=min(max(q.x / self.w, 0.0), 1.0), y=min(max(q.y / self.h, 0.0), 1.0))

    def region(self, storey: Storey, building: Building) -> Box:
        pts = [p for w in storey.walls for p in (w.start, w.end)]
        for d in building.decks:
            pts += d.outline.points
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        m = PLAN_MARGIN_MM + (building.roof.eaves_mm or 0)
        a = self.pt(Pt(x=min(xs) - m, y=max(ys) + m))
        b = self.pt(Pt(x=max(xs) + m, y=min(ys) - m))
        return Box(x1=a.x, y1=a.y, x2=b.x, y2=b.y)


def page_sizes_from_pdf(pdf_bytes: bytes) -> dict[int, tuple[float, float]]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    return {i + 1: (p.rect.width, p.rect.height) for i, p in enumerate(doc)}


# ---------------------------------------------------------------------------
# Notes library
# ---------------------------------------------------------------------------


def load_notes(path: Path = NOTES_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def _facts(project: Project, b: Building) -> dict[str, Any]:
    masonry = b.external_wall_construction in {"brick_veneer", "double_brick", "blockwork", "blockwork_corefilled"}
    columns = any(s.columns for s in b.storeys)
    suspended = any(s.floor.kind in {"suspended_timber", "suspended_timber_on_steel"} for s in b.storeys)
    return {
        "has_steel": columns or b.roof.construction == "unknown" and False,
        "masonry_veneer_or_full": masonry,
        "has_bolted_timber": bool(b.decks) or (columns and suspended),
        "bearers_on_columns": columns and suspended,
        "roof_trussed": b.roof.construction == "truss",
        "storeys": b.storey_count,
    }


def _when_ok(when: dict | None, facts: dict[str, Any]) -> bool:
    if not when:
        return True
    return all(facts.get(k) == v for k, v in when.items())


def sheet_notes(kind: str, project: Project, b: Building, lib: dict) -> list[str]:
    facts = _facts(project, b)
    cats = SHEET_CATEGORIES.get(kind, set())
    out: list[str] = []
    for n in lib.get("notes", []):
        sheets = set(n.get("sheets", []))
        if ("all" in sheets or sheets & cats) and _when_ok(n.get("when"), facts):
            out.append("-" + n["text"].upper())
    return out


def notice_to_owner(lib: dict) -> list[str]:
    n = lib.get("notice_to_owner")
    return [n["title"].upper(), n["text"].upper()] if n else []


# ---------------------------------------------------------------------------
# Footing sheet
# ---------------------------------------------------------------------------


def _fmt(v: Any) -> str:
    return "" if v is None else str(v)


def _strip_text(p: dict) -> str:
    return f"{_fmt(p.get('width_mm'))} W x MIN. {_fmt(p.get('depth_mm'))} DEEP STRIP FOOTING {_fmt(p.get('reinforcement'))}".upper()


def _pier_text(p: dict, with_plate: bool = False) -> str:
    base = f"{_fmt(p.get('diameter_mm'))} BORED PIER {_fmt(p.get('founding'))}".upper()
    if with_plate:
        return base + " WITH BASE PLATE FOR SHS ABOVE"
    tail = " AT CORNERS AND MAX {}¢".format(int(p["max_spacing_mm"])) if p.get("max_spacing_mm") else ""
    return base + tail + f" {_fmt(p.get('reinforcement'))}".upper()


def _lb_walls(storey: Storey) -> list[Wall]:
    return [w for w in storey.walls if w.external or w.load_bearing]


def footing_elements(b: Building, storey: Storey, result: Any, m: Mapper) -> tuple[list[Element], list[ScheduleItem], list[str], list[str]]:
    """Returns (elements, schedule, extra notes, warnings) for the footing plan."""
    els: list[Element] = []
    sched: list[ScheduleItem] = []
    notes: list[str] = []
    warn: list[str] = []
    if result is None or not result.proposal:
        warn.append(f"{b.id}: footings engine gave no proposal ({getattr(result, 'status', 'not run')})")
        return els, sched, notes, warn
    p = result.proposal
    system = result.footing_system or ""
    lb = _lb_walls(storey)

    if system.startswith("strip") or system == "piers_to_rock":
        tag = p.get("tag", "SF1")
        for w in lb:
            els.append(Element(kind="member", tag=tag, start=m.pt(w.start), end=m.pt(w.end), text=""))
        sched.append(ScheduleItem(tag=tag, text=_strip_text(p)))
        piers = p.get("piers")
        if piers and piers.get("max_spacing_mm"):
            col_pts = [c.at for c in storey.columns]
            for q in place_piers(lb, float(piers["max_spacing_mm"]), point_loads=col_pts):
                under_col = any(math.hypot(q.x - c.x, q.y - c.y) < 300 for c in col_pts)
                ptag = "P1" if under_col else piers.get("tag", "P2")
                els.append(Element(kind="pier", tag=ptag, start=m.pt(q), end=m.pt(q), text=""))
            sched.append(ScheduleItem(tag=piers.get("tag", "P2"), text=_pier_text(piers)))
            if col_pts:
                sched.insert(0, ScheduleItem(tag="P1", text=_pier_text(piers, with_plate=True)))
            first = lb[0].start if lb else None
            if first is not None:
                els.append(Element(kind="callout", tag="", start=m.pt(first), end=m.pt(first),
                                   text=f"Ø{_fmt(piers.get('diameter_mm'))} BORED PIER AT CORNERS AND MAX {int(piers['max_spacing_mm'])}¢ UNDER {tag} (TYPICAL)"))
        if p.get("on_rock_note"):
            notes.append("-" + str(p["on_rock_note"]).upper())
    elif system in {"stiffened_raft", "footing_slab"}:
        eb, ib = p.get("edge_beam_tag", "EB1"), p.get("internal_beam_tag", "IB1")
        for w in lb:
            els.append(Element(kind="member", tag=eb if w.external else ib, start=m.pt(w.start), end=m.pt(w.end), text=""))
        sched.append(ScheduleItem(tag=eb, text=f"{_fmt(p.get('edge_beam_width_mm'))} W x MIN. {_fmt(p.get('edge_beam_depth_mm'))} DEEP EDGE BEAM {_fmt(p.get('edge_beam_reinforcement'))}".upper()))
        if any(not w.external for w in lb):
            sched.append(ScheduleItem(tag=ib, text=f"{_fmt(p.get('internal_beam_width_mm'))} W x MIN. {_fmt(p.get('internal_beam_depth_mm'))} DEEP INTERNAL BEAM {_fmt(p.get('internal_beam_reinforcement'))}".upper()))
        if p.get("slab_thickness_mm"):
            xs = [q.x for w in storey.walls for q in (w.start, w.end)]
            ys = [q.y for w in storey.walls for q in (w.start, w.end)]
            centre = Pt(x=(min(xs) + max(xs)) / 2, y=(min(ys) + max(ys)) / 2)
            els.append(Element(kind="callout", tag="", start=m.pt(centre), end=m.pt(centre),
                               text=f"{p['slab_thickness_mm']} THICK REINFORCED CONCRETE SLAB. {_fmt(p.get('slab_mesh'))}. {_fmt(p.get('concrete'))} GRADE.".upper()))
        piers = p.get("piers")
        if piers:
            sched.append(ScheduleItem(tag=piers.get("tag", "P3"), text=_pier_text(piers)))
    else:
        warn.append(f"{b.id}: footing system '{system}' has no drawing rule yet")

    for c in storey.columns:
        if c.section:
            sched.append(ScheduleItem(tag=c.id, text=f"{c.section} COLUMN".upper()))
    if not getattr(result, "verified", False):
        notes.append("-FOOTING SIZES ARE THE PRACTICE STANDARD; ENGINEER TO VERIFY AGAINST AS 2870 BEFORE ISSUE")
        warn.append(f"{b.id}: footings unverified - " + "; ".join(result.blocked_on or ["standard design table not loaded"]))
    return els, sched, notes, warn


# ---------------------------------------------------------------------------
# Roof / wall sheets
# ---------------------------------------------------------------------------


def roof_elements(b: Building, storey: Storey, m: Mapper) -> tuple[list[Element], list[ScheduleItem], list[str]]:
    els: list[Element] = []
    sched: list[ScheduleItem] = []
    warn: list[str] = []
    xs = [q.x for w in storey.walls for q in (w.start, w.end)]
    ys = [q.y for w in storey.walls for q in (w.start, w.end)]
    if b.roof.construction == "truss":
        a, c = Pt(x=min(xs), y=min(ys)), Pt(x=max(xs), y=max(ys))
        els.append(Element(kind="span", tag="TR1", start=m.pt(a), end=m.pt(c), text=""))
        sched.append(ScheduleItem(tag="TR1", text="ROOF TRUSSES BY MANUFACTURER"))
    else:
        warn.append(f"{b.id}: rafter roof members pending (roof_members_as1684 engine)")
    for c in storey.columns:
        if c.section:
            sched.append(ScheduleItem(tag=c.id, text=f"{c.section} COLUMN".upper()))
    return els, sched, warn


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------


def _storey_for(kind: str, b: Building) -> Storey | None:
    by_name = {s.name: s for s in b.storeys}
    if kind in {"floor_ff", "wall_ff"}:
        return by_name.get("first")
    if kind == "roof" and "first" in by_name:
        return by_name["first"]
    return by_name.get("ground") or (b.storeys[0] if b.storeys else None)


def annotate(
    project: Project,
    run: DesignRun,
    page_sizes: dict[int, tuple[float, float]],
    prefs: dict,
    notes_lib: dict | None = None,
) -> list[SheetOut]:
    notes_lib = notes_lib or load_notes()
    banner = prefs.get("banner", {})
    wind = run.result("wind_as4055")
    wind_class = (wind.wind_class if wind else None) or project.site.wind.stated_class or "?"
    out: list[SheetOut] = []

    for b in project.buildings:
        for kind in run.plan.sheets.get(b.id, []):
            storey = _storey_for(kind, b)
            warnings: list[str] = []
            if storey is None or storey.frame is None:
                out.append(SheetOut(b.id, kind, SHEET_TITLES_V2[kind], MarkupSheet(
                    source_page_number=1, plan_region=Box(x1=0, y1=0, x2=1, y2=1), classification="", schedule_title="",
                    schedule=[], legend_title="", legend=[], elements=[], notes=[]), [f"{b.id}/{kind}: no plan frame"]))
                continue
            size = page_sizes.get(storey.frame.page_number)
            if size is None:
                out.append(SheetOut(b.id, kind, SHEET_TITLES_V2[kind], MarkupSheet(
                    source_page_number=storey.frame.page_number, plan_region=Box(x1=0, y1=0, x2=1, y2=1), classification="",
                    schedule_title="", schedule=[], legend_title="", legend=[], elements=[], notes=[]),
                    [f"{b.id}/{kind}: page size unknown for page {storey.frame.page_number}"]))
                continue
            m = Mapper(storey, size)
            els: list[Element] = []
            sched: list[ScheduleItem] = []
            notes = sheet_notes(kind, project, b, notes_lib)
            legend: list[ScheduleItem] = []
            legend_title = ""

            if kind in FOOTING_SHEETS:
                classification = banner.get("footing_sheets", "AS 2870:2011 SITE CLASSIFICATION: {site_class}").format(site_class=project.site.geotech.site_class)
                fe, fs, fn, fw = footing_elements(b, storey, run.result("footings_as2870", b.id), m)
                els += fe
                sched += fs
                notes += fn
                warnings += fw
                notes += notice_to_owner(notes_lib)
                schedule_title = "FOOTING SCHEDULE:"
                if kind == "footing_floor":
                    warnings.append(f"{b.id}: floor framing members pending (floor_members_as1684 engine)")
            else:
                classification = banner.get("framing_sheets", "AS 4055:2021 WIND CLASSIFICATION: {wind_class}").format(wind_class=wind_class)
                if kind.startswith("roof"):
                    re_, rs, rw = roof_elements(b, storey, m)
                    els += re_
                    sched += rs
                    warnings += rw
                    schedule_title = "ROOF FRAMING SCHEDULE:"
                else:
                    schedule_title = "LINTEL SCHEDULE:"
                    for c in storey.columns:
                        if c.section:
                            sched.append(ScheduleItem(tag=c.id, text=f"{c.section} COLUMN".upper()))
                if kind.startswith("wall") or kind == "roof_wall":
                    warnings.append(f"{b.id}/{kind}: bracing and lintels pending (racking_as1684 / lintels_as1684 engines)")
                    legend_title = "BRACING LEGEND:"
                    for code, t in (prefs.get("bracing", {}).get("types_used") or {}).items():
                        legend.append(ScheduleItem(tag=code, text=str(t.get("description", "")).upper()))

            sheet = MarkupSheet(
                source_page_number=storey.frame.page_number,
                plan_region=m.region(storey, b),
                classification=classification,
                schedule_title=schedule_title,
                schedule=sched,
                legend_title=legend_title,
                legend=legend,
                elements=els,
                notes=notes,
            )
            title = SHEET_TITLES_V2[kind] + (f" - {b.name or b.id}".upper() if len(project.buildings) > 1 else "")
            out.append(SheetOut(b.id, kind, title, sheet, warnings))
    return out
