"""Geometry placement: where things go on the plan once the engines have said what they are.

Pure functions over the model. The *rules* (maximum pier spacing, bracing spacing limits,
corner zones) are passed in - they come from the standards tables / preferences loaded
by the engines, never from constants here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..model import Opening, Pt, Storey, Wall

# ---------------------------------------------------------------------------
# Small geometry helpers
# ---------------------------------------------------------------------------


def _len(a: Pt, b: Pt) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def _lerp(a: Pt, b: Pt, t: float) -> Pt:
    return Pt(x=a.x + (b.x - a.x) * t, y=a.y + (b.y - a.y) * t)


def _close(a: Pt, b: Pt, tol: float = 50) -> bool:
    return _len(a, b) <= tol


def wall_direction(w: Wall) -> str:
    """'x' if the wall runs predominantly left-right, else 'y'."""
    return "x" if abs(w.end.x - w.start.x) >= abs(w.end.y - w.start.y) else "y"


# ---------------------------------------------------------------------------
# Piers along footings
# ---------------------------------------------------------------------------


def place_piers(
    walls: list[Wall],
    max_spacing_mm: float,
    point_loads: list[Pt] | None = None,
    dedupe_tol_mm: float = 300,
) -> list[Pt]:
    """Piers at every wall end / corner, under every point load, and at equal spacing not
    exceeding `max_spacing_mm` along each wall. Coincident piers are merged.

    This is the practice's rule "piers at corners and max N ctrs" expressed in geometry;
    the spacing value comes from the footing engine (AS 2870 / practice standard).
    """
    piers: list[Pt] = []

    def add(p: Pt) -> None:
        if not any(_close(p, q, dedupe_tol_mm) for q in piers):
            piers.append(p)

    for p in point_loads or []:
        add(p)
    for w in walls:
        L = _len(w.start, w.end)
        add(w.start)
        add(w.end)
        if L > max_spacing_mm:
            n = math.ceil(L / max_spacing_mm)
            for i in range(1, n):
                add(_lerp(w.start, w.end, i / n))
    return piers


# ---------------------------------------------------------------------------
# Bracing panels
# ---------------------------------------------------------------------------


@dataclass
class PanelType:
    code: str  # e.g. "d", "h(B)", "g"
    capacity_kn_per_m: float
    min_length_mm: float
    lengths_mm: list[float]  # lengths the practice draws, ascending
    max_length_mm: float | None = None


@dataclass
class BracingRules:
    max_spacing_mm: float  # between bracing walls in a direction (AS 1684.2 cl 8.3.6)
    corner_zone_mm: float  # a panel wanted within this distance of each external corner
    min_external_share: float = 0.5  # share of capacity to place in external walls (0 disables)


@dataclass
class Segment:
    wall_id: str
    start: Pt
    end: Pt
    external: bool
    offset_mm: float  # from wall start

    @property
    def length_mm(self) -> float:
        return _len(self.start, self.end)


@dataclass
class Panel:
    wall_id: str
    start: Pt
    end: Pt
    type: PanelType
    length_mm: float
    external: bool

    @property
    def capacity_kn(self) -> float:
        return self.type.capacity_kn_per_m * self.length_mm / 1000

    @property
    def label(self) -> str:
        return f"{self.type.code} - {int(self.length_mm)}"

    @property
    def mid(self) -> Pt:
        return _lerp(self.start, self.end, 0.5)


@dataclass
class BracingResult:
    direction: str
    demand_kn: float
    panels: list[Panel] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def capacity_kn(self) -> float:
        return sum(p.capacity_kn for p in self.panels)

    @property
    def adequate(self) -> bool:
        return self.capacity_kn >= self.demand_kn


def solid_segments(wall: Wall, min_length_mm: float) -> list[Segment]:
    """Full-height wall between openings - the only places a bracing panel can go."""
    L = _len(wall.start, wall.end)
    cuts: list[tuple[float, float]] = sorted((o.offset_mm, o.offset_mm + o.width_mm) for o in wall.openings)
    segs: list[Segment] = []
    pos = 0.0
    for a, b in cuts + [(L, L)]:
        if a - pos >= min_length_mm:
            segs.append(Segment(wall.id, _lerp(wall.start, wall.end, pos / L), _lerp(wall.start, wall.end, a / L), wall.external, pos))
        pos = max(pos, b)
    return segs


def _best_panel(seg_len: float, types: list[PanelType]) -> tuple[PanelType, float] | None:
    """Longest standard length of the most-preferred type that fits the segment."""
    for t in types:  # types in preference order
        fits = [l for l in t.lengths_mm if l <= seg_len and l >= t.min_length_mm and (t.max_length_mm is None or l <= t.max_length_mm)]
        if fits:
            return t, max(fits)
    return None


def _coord(p: Pt, direction: str) -> float:
    """Position across the building, perpendicular to the bracing direction."""
    return p.y if direction == "x" else p.x


def place_bracing(
    storey: Storey,
    direction: str,
    demand_kn: float,
    types: list[PanelType],
    rules: BracingRules,
    footprint_extent: tuple[float, float] | None = None,
) -> BracingResult:
    """Place bracing panels in walls running along `direction` until capacity ≥ demand,
    honouring corner zones and the maximum spacing between braced lines.

    Order of preference: (1) a panel near each external corner, (2) the longest segments
    in external walls, (3) internal bracing-eligible walls where needed for spacing or
    capacity. The result lists every panel with its wall, position and capacity so the
    trace can show the arithmetic.
    """
    res = BracingResult(direction=direction, demand_kn=demand_kn)
    walls = [w for w in storey.walls if wall_direction(w) == direction and w.bracing_eligible and w.construction == "timber_stud"]
    segs = [s for w in walls for s in solid_segments(w, min(t.min_length_mm for t in types))]
    if not segs:
        res.notes.append(f"no bracing-eligible timber walls run along {direction}")
        return res

    # Lines across the building where braced walls sit (for the spacing check).
    lines = sorted({round(_coord(s.start, direction), -1) for s in segs})
    lo, hi = (footprint_extent if footprint_extent else (min(lines), max(lines)))
    used: set[tuple[str, float]] = set()

    def take(seg: Segment, reason: str) -> bool:
        key = (seg.wall_id, round(seg.offset_mm))
        if key in used:
            return False
        bp = _best_panel(seg.length_mm, types)
        if bp is None:
            return False
        t, L = bp
        # Centre the panel in the segment unless it sits at a wall end (then flush to the end).
        slack = seg.length_mm - L
        at_start = _close(seg.start, next(w for w in walls if w.id == seg.wall_id).start)
        at_end = _close(seg.end, next(w for w in walls if w.id == seg.wall_id).end)
        t0 = 0.0 if at_start else (slack / seg.length_mm if at_end else slack / 2 / seg.length_mm)
        a = _lerp(seg.start, seg.end, t0)
        b = _lerp(seg.start, seg.end, t0 + L / seg.length_mm)
        res.panels.append(Panel(seg.wall_id, a, b, t, L, seg.external))
        res.notes.append(f"{seg.wall_id}: {t.code} - {int(L)} ({reason})")
        used.add(key)
        return True

    # 1. Corner zones: the extreme braced lines, and the ends of each.
    ext_segs = [s for s in segs if s.external]
    for target in (lo, hi):
        near = [s for s in ext_segs if abs(_coord(s.start, direction) - target) <= rules.corner_zone_mm]
        near.sort(key=lambda s: -s.length_mm)
        for s in near[:2]:
            take(s, "corner zone")

    # 2. Spacing: walk across the building; where the gap between braced lines exceeds the
    #    maximum, brace the best segment inside the gap.
    def braced_lines() -> list[float]:
        return sorted({round(_coord(p.start, direction), -1) for p in res.panels})

    guard = 0
    while guard < 50:
        guard += 1
        bl = braced_lines()
        gaps = [(lo, bl[0] if bl else hi)] + list(zip(bl, bl[1:])) + ([(bl[-1], hi)] if bl else [])
        gap = next(((a, b) for a, b in gaps if b - a > rules.max_spacing_mm), None)
        if gap is None:
            break
        inside = [s for s in segs if gap[0] < _coord(s.start, direction) < gap[1]]
        inside.sort(key=lambda s: (-s.external, -s.length_mm))
        if not any(take(s, f"spacing > {int(rules.max_spacing_mm)} between braced lines") for s in inside):
            res.notes.append(f"gap {int(gap[0])}–{int(gap[1])} exceeds max spacing and has no eligible wall - engineer to resolve")
            break

    # 3. Capacity: add panels, external first, longest first, until demand is met.
    for s in sorted(segs, key=lambda s: (-s.external, -s.length_mm)):
        if res.adequate:
            break
        take(s, "capacity")

    if not res.adequate:
        res.notes.append(f"capacity {res.capacity_kn:.1f} kN < demand {demand_kn:.1f} kN - add bracing (steel portal / extra walls) - engineer")
    if rules.min_external_share and res.panels:
        ext = sum(p.capacity_kn for p in res.panels if p.external)
        if ext < rules.min_external_share * res.capacity_kn:
            res.notes.append(f"only {ext / res.capacity_kn:.0%} of bracing in external walls - check distribution")
    return res


# ---------------------------------------------------------------------------
# Lintel tagging
# ---------------------------------------------------------------------------


@dataclass
class LintelInstance:
    wall_id: str
    opening: Opening
    size: str  # designed size, e.g. "190x45 F17/LVL"
    tag: str = ""


def assign_lintel_tags(instances: list[LintelInstance], size_order: list[str]) -> dict[str, str]:
    """The practice numbers lintels by size, smallest first: L1 90x35 ... Ln. Returns
    {size: tag} and writes tags onto the instances. Sizes not in `size_order` go last."""
    sizes = sorted({i.size for i in instances}, key=lambda s: (size_order.index(s.split()[0]) if s.split()[0] in size_order else 99, s))
    tags = {s: f"L{i + 1}" for i, s in enumerate(sizes)}
    for i in instances:
        i.tag = tags[i.size]
    return tags
