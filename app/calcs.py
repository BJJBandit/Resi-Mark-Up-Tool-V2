"""Calculation sheet: the design basis, the racking (bracing) check and Claude's working
for the key members. The racking force for each wind direction and the capacity of each
bracing type come from the design assessment (AS 1684.2 Section 8); the bracing provided
is totalled here from the panels actually drawn on the wall framing plan."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import spelling
from .schemas import DesignAssessment, GeotechReview, MarkupSheet
from .snap import bracing_length_mm

METHOD = [
    "HOW THE TIMBER BRACING IS DETERMINED (AS 1684.2 SECTION 8)",
    "1. WIND CLASSIFICATION TO AS 4055 (SITE / GEOTECHNICAL REPORT OR AS ENTERED).",
    "2. AREA OF ELEVATION FOR EACH WIND DIRECTION (CL 8.3.3, FIG 8.2): THE ELEVATION OF THE BUILDING "
    "FROM HALF THE STOREY'S WALL HEIGHT UP TO THE ROOF, INCLUDING THE ROOF AND GABLES, FOR WIND ON EACH "
    "FACE (EACH STOREY SEPARATELY).",
    "3. PRESSURE ON THE AREA OF ELEVATION (TABLES 8.1 TO 8.5) FOR THE WIND CLASS, BUILDING WIDTH, "
    "ROOF PITCH AND ROOF FORM.",
    "4. RACKING FORCE = AREA OF ELEVATION x PRESSURE (kN), FOR EACH DIRECTION.",
    "5. BRACING CAPACITY = SUM OF (BRACING WALL LENGTH x CAPACITY PER METRE, TABLE 8.18) FOR THE "
    "BRACING WALLS PARALLEL TO THE WIND. IT MUST BE AT LEAST THE RACKING FORCE IN EACH DIRECTION.",
    "6. BRACING WALLS DISTRIBUTED THROUGH THE BUILDING IN BOTH DIRECTIONS AT NO MORE THAN THE "
    "SPACING OF CL 8.3.6, WITH HOLD-DOWNS / FIXINGS TO CL 8.3.6.9 AND TABLE 8.18. PLASTERBOARD "
    "NOMINAL BRACING IS NOT RELIED ON UNLESS NOTED.",
]


def _type_key(tag: str) -> str:
    """'h(b) - 900' -> 'h(b)'."""
    return re.split(r"\s*-\s*\d", (tag or "").strip().lower(), maxsplit=1)[0].replace(" ", "")


@dataclass
class DirectionCheck:
    wind: str
    walls: str
    required_kn: float
    provided_kn: float
    panels: dict[str, list[float]] = field(default_factory=dict)  # type -> lengths (mm)
    missing: list[str] = field(default_factory=list)  # bracing types with no capacity given

    @property
    def ok(self) -> bool:
        return self.provided_kn >= self.required_kn - 1e-6


def bracing_check(design: DesignAssessment, walls: MarkupSheet | None, aspect: float = 1.414) -> list[DirectionCheck]:
    """Bracing provided on the wall framing plan against the racking force, each direction.
    aspect = source page width / height (to tell left-right panels from top-bottom ones)."""
    caps: dict[str, float] = {}
    for c in design.bracing_capacities:
        caps[_type_key(c.type)] = c.kn_per_m
    out = []
    panels = [e for e in (walls.elements if walls else []) if e.kind == "bracing"]
    for r in design.racking:
        across = r.bracing_walls == "across the page"
        chk = DirectionCheck(r.wind_direction, r.bracing_walls, r.force_kn, 0.0)
        for e in panels:
            dx, dy = (e.end.x - e.start.x) * aspect, e.end.y - e.start.y
            if (abs(dx) >= abs(dy)) != across:
                continue
            mm = bracing_length_mm(e.tag)
            if not mm:
                continue
            key = _type_key(e.tag)
            cap = caps.get(key)
            if cap is None:  # 'h(b)' scheduled as 'h' (or the reverse): match on the letter
                cap = next((v for k, v in caps.items() if k[:1] == key[:1]), None)
            chk.panels.setdefault(key, []).append(mm)
            if cap is None:
                if key not in chk.missing:
                    chk.missing.append(key)
                continue
            chk.provided_kn += cap * mm / 1000
        out.append(chk)
    return out


def warnings(checks: list[DirectionCheck]) -> list[str]:
    out = []
    for c in checks:
        if not c.panels:
            out.append(f"Calculation sheet: no bracing drawn {c.walls} for {c.wind.lower()} "
                       f"(racking {c.required_kn:.1f} kN)")
        elif not c.ok:
            out.append(f"Calculation sheet: bracing {c.walls} {c.provided_kn:.1f} kN is less than the racking "
                       f"force {c.required_kn:.1f} kN for {c.wind.lower()} - add bracing")
        if c.missing:
            out.append(f"Calculation sheet: no capacity given for bracing type(s) {', '.join(c.missing)} - "
                       "not counted")
    return out


_ASCII = {"→": "->", "⇒": "->", "≈": "~", "≃": "~", "−": "-", "–": "-", "—": "-", "×": "x", "≥": ">=",
          "≤": "<=", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "..."}


def _plain(text: str) -> str:
    """Characters the sheet's PDF font shows (no arrows / approx signs)."""
    return "".join(_ASCII.get(ch, ch) for ch in text)


def _brief(text: str, n: int = 90) -> str:
    """First clause of a long description, at most about n characters."""
    t = " ".join(str(text or "").split())
    # A clause break - not the dash of a range ('0 - 20 mm') or the point of a number ('2.5').
    m = re.search(r"(?<!\d)\.\s|;\s|\s\(|,\s+WITH\s|(?<!\d)\s-\s|\s-\s(?!\d)", t, re.I)
    if m and m.start() > 0:
        t = t[:m.start()]
    if len(t) > n:
        t = t[:n].rsplit(" ", 1)[0] + " ..."
    return t.rstrip(" .,;")


def _bracing_item(topic: str) -> bool:
    """Claude's own racking / bracing working - the app's check (from the panels actually
    drawn) replaces it, so the sheet never shows two different totals."""
    return re.search(r"RACKING|BRACING", topic or "", re.I) is not None


def _f(x: float) -> str:
    return f"{x:.1f}"


def lines(geotech: GeotechReview, design: DesignAssessment, checks: list[DirectionCheck],
          references: list[str], mismatches: dict[int, list[tuple[str, str]]] | None = None) -> list[str]:
    """The calculation sheet as text lines (blank line between items; upper case, house style)."""
    L: list[str] = ["DESIGN BASIS", ""]
    L += [f"-SITE CLASSIFICATION: {_brief(geotech.site_classification, 40)} (AS 2870), {_brief(geotech.ys, 60)}.",
          f"-WIND CLASSIFICATION: {_brief(design.wind_classification_used or geotech.wind_classification, 40)} "
          "(AS 4055).",
          f"-STRUCTURE: {design.storeys} STOREY, {_brief(design.external_wall_type, 50)}, "
          f"{_brief(design.framing_material, 30)} FRAMING, {_brief(design.roof_form, 40)} ROOF, "
          f"{_brief(design.roof_construction, 50)}, PITCH {_brief(design.roof_pitch, 25)}, "
          f"{_brief(design.roof_cladding, 40)}, WALL HEIGHT {_brief(design.wall_height, 30)}.",
          f"-GOVERNING STANDARD: {_brief(design.governing_standard, 140)}.",
          "-STANDARDS / GUIDES SUPPLIED: " + ("; ".join(references) if references else "NONE - VALUES TO THE "
                                             "CURRENT EDITIONS OF THE STANDARDS, ENGINEER TO CONFIRM") + ".",
          ""]
    L += METHOD + [""]
    L += ["RACKING FORCES", ""]
    if not design.racking:
        L += ["-NOT CALCULATED IN THIS REVIEW: " + (design.racking_summary or "SEE NOTES") + ".", ""]
    for r in design.racking:
        L += [f"{r.wind_direction} ({r.storey}) - RESISTED BY WALLS {r.bracing_walls}",
              f"-AREA OF ELEVATION: {r.area_working} = {_f(r.area_m2)} m2",
              f"-PRESSURE: {r.pressure_kpa:.2f} kPa ({r.pressure_ref}), W = {_f(r.width_m)} m, "
              f"PITCH {r.roof_pitch_deg:.1f} DEG",
              f"-RACKING FORCE = {_f(r.area_m2)} x {r.pressure_kpa:.2f} = {_f(r.force_kn)} kN", ""]
    L += ["BRACING CAPACITIES (AS 1684.2 TABLE 8.18)", ""]
    L += [f"-{c.type}: {c.description} - {c.kn_per_m:.2f} kN/m ({c.ref})" for c in design.bracing_capacities] \
        or ["-NONE GIVEN"]
    L += [""]
    L += ["BRACING CHECK - PANELS ON THE WALL FRAMING PLAN", ""]
    caps = {_type_key(c.type): c.kn_per_m for c in design.bracing_capacities}
    for c in checks:
        L.append(f"{c.wind} - WALLS {c.walls}")
        for key, lengths in sorted(c.panels.items()):
            cap = caps.get(key, next((v for k, v in caps.items() if k[:1] == key[:1]), None))
            total = sum(lengths) / 1000
            if cap is None:
                L.append(f"-{key}: {len(lengths)} PANEL(S), {total:.2f} m - NO CAPACITY GIVEN, NOT COUNTED")
            else:
                L.append(f"-{key}: {len(lengths)} PANEL(S), {' + '.join(f'{x / 1000:.1f}' for x in lengths)} = "
                         f"{total:.2f} m x {cap:.2f} kN/m = {_f(total * cap)} kN")
        if not c.panels:
            L.append("-NO BRACING PANELS DRAWN IN THIS DIRECTION")
        L.append(f"-PROVIDED {_f(c.provided_kn)} kN {'>=' if c.ok else '<'} REQUIRED {_f(c.required_kn)} kN - "
                 + ("OK" if c.ok and c.panels else "NOT ADEQUATE - ADD BRACING"))
        L.append("")
    if not checks:
        L += ["-NO RACKING FORCES TO CHECK.", ""]
    members = [(i, c) for i, c in enumerate(design.calculations) if not _bracing_item(c.topic)]
    if members:
        L += ["MEMBER AND FOOTING CALCULATIONS", ""]
        for i, c in members:
            L += [c.topic, f"-BASIS: {c.basis}", f"-WORKING: {c.working}", f"-RESULT: {c.result}"]
            for tag, text in (mismatches or {}).get(i, []):
                L.append(f"-CHECK: THE SCHEDULE SHOWS {text} - DIFFERENT FROM THIS RESULT. ENGINEER TO RESOLVE.")
            L.append("")
    L += ["PRELIMINARY CALCULATIONS FOR THE MARK-UP - TO BE VERIFIED BY THE DESIGN ENGINEER."]
    return [spelling.tidy(_plain(x).upper()) for x in L]


_RACKING_NOTE = re.compile(r"RACKING|BRACING", re.I)
_TOTALS = re.compile(r"PROVIDED|REQUIRED|\d\s*kN\b", re.I)


def racking_notes(design: DesignAssessment, walls: MarkupSheet, aspect: float) -> list[str]:
    """The wall sheet's notes with Claude's racking / bracing totals replaced by one note
    from the bracing check (the panels as drawn), so the notes and the calculation sheet
    agree. Other notes are kept as written."""
    checks = bracing_check(design, walls, aspect)
    if not checks:
        return list(walls.notes)
    kept = [n for n in walls.notes if not (_RACKING_NOTE.search(n) and _TOTALS.search(n))]
    parts = [f"{c.wind} - {c.required_kn:.1f} kN REQUIRED, {c.provided_kn:.1f} kN PROVIDED "
             f"({'OK' if c.ok and c.panels else 'NOT ADEQUATE - ADD BRACING'})" for c in checks]
    note = spelling.tidy(("-RACKING TO AS 1684.2 SECTION 8: " + "; ".join(parts) + ". REFER CALCULATION SHEET.").upper())
    return kept + [note]


_SIZE = re.compile(r"(?:\d/)?\d{2,3}X\d{1,3}(?:\.\d+)?|\d{3}\s?PFC|\d{3}UB\d+(?:\.\d+)?|\d{3}X\d{3}X\d{1,2}")


def _sizes(text: str) -> set[str]:
    return {m.replace(" ", "") for m in _SIZE.findall((text or "").upper())}


def schedule_mismatches(design: DesignAssessment, sheets: dict) -> dict[int, list[tuple[str, str]]]:
    """Member calculations whose result names a different section from the schedule entry of
    a tag named in their topic (e.g. calc 'RB1 ... 2/290x45' vs schedule 'RB1: 290x35').
    Returns {index in design.calculations: [(tag, schedule text)]}."""
    sched: dict[str, str] = {}
    for sh in sheets.values():
        for item in getattr(sh, "schedule", []) or []:
            tag = item.tag.strip().upper()
            if tag and _sizes(item.text):
                sched.setdefault(tag, item.text.upper())
    out: dict[int, list[tuple[str, str]]] = {}
    for i, c in enumerate(design.calculations):
        got = _sizes(c.result)
        if not got:
            continue
        for tag, text in sched.items():
            if re.search(rf"(?<![A-Z0-9]){re.escape(tag)}(?![A-Z0-9])", (c.topic or "").upper()) and \
                    not (_sizes(text) & got):
                out.setdefault(i, []).append((tag, text))
    return out
