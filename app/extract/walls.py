"""Step 4 - walls, openings and the plan regions of one page.

Decision logic, in order:

1. **Wall bars.** Two kinds of evidence, used one or the other per page:
   * *fill mode* - the office draws walls as solid dark bars (one filled rectangle
     per wall segment between openings). If the page holds enough of them, they are
     the walls, full stop.
   * *pair mode* - walls are two parallel lines drawn with the heaviest pen on the
     page. Every pair of such lines at a wall-like spacing (60-320 mm) that overlap
     along their length is a candidate. Pairs with two or more heavy lines between
     them (a double wall, a bench) and pairs that are part of an evenly spaced
     repeat (stairs, tiles, hatching) are rejected. Candidates are then accepted
     greedily - nominal thickness first, thicker first - and each accepted wall
     consumes its two lines so the brick skin of a brick-veneer wall is not also
     reported as a 110 wall of its own.
2. **Runs.** Collinear bars of the same thickness are chained into runs. A gap
   between two bars is bridged silently when a cross wall passes through it, is an
   opening when there is evidence (a tag, window lines in the gap, a door swing) or
   when the gap is not boxed in by cross walls at both ends, and otherwise ends the
   run.
3. **Corners.** Run ends are stretched to the centre-line of the cross wall they
   meet, so centre-lines join.
4. **Plans.** The union of the wall footprints (shapely) splits the page into
   connected clusters; each cluster bigger than 2.5 m x 2.5 m with at least four
   walls is a plan. A wall is external when open air lies along one of its faces,
   i.e. a point just off that face falls outside the cluster's filled outline.
5. **Tags.** Each opening takes the nearest unused tag text; tags that encode a
   width give the opening its width and kind.

Failure modes: walls drawn with the same pen as furniture and no fill (neither
mode applies - reported); a perimeter with a side that has no wall at all (open
carport) lets the "outside" leak in and marks some internal walls external;
curved or diagonal walls are not detected (axis-aligned only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from .geom import Axis, Bar, Seg, overlap, pts_per_mm
from .pdf_pages import Line, PageScan
from .tags import Tag, looks_like_tag, parse_tag
from .vector import VectorSet

NOMINAL_MM = (90, 250, 190, 110, 230, 270, 290, 140)  # preference order when two are within tolerance
NOMINAL_TOL_MM = 15
MIN_T_MM, MAX_T_MM = 60, 320
MIN_LINE_MM = 350  # a heavy line shorter than this is never a wall face
MIN_PIECE_MM = 250  # minimum overlap for a line pair
MAX_OPENING_MM = 7000
MIN_OPENING_MM = 300
UNTAGGED_MAX_OPENING_MM = 2500  # a gap with no tag, window lines or swing is only a door-sized opening
JUNK_PIECE_MM = 1500  # a non-nominal pair piece shorter than this is joinery, not a wall
SILENT_GAP_MM = 70  # gaps up to this are drafting noise
PLAN_MIN_SIZE_MM = 2500
PLAN_MIN_WALLS = 4
FILL_MODE_MIN_LENGTH_MM = 15_000
TAG_REACH_MM = 1100


@dataclass
class OpeningCandidate:
    lo: float  # along the run, points
    hi: float
    kind: str = "unknown"
    tag: Tag | None = None
    tag_text: str = ""
    evidence: str = ""  # "tag", "window_lines", "arc", "no_junction"
    confidence: float = 0.5

    @property
    def width_pt(self) -> float:
        return self.hi - self.lo


@dataclass
class Run:
    axis: Axis
    pos: float
    lo: float
    hi: float
    thickness: float  # points, measured
    pieces: list[Bar]
    openings: list[OpeningCandidate] = field(default_factory=list)
    external: bool | None = None
    id: str = ""
    nominal_mm: float | None = None

    @property
    def length(self) -> float:
        return self.hi - self.lo

    def point(self, along: float) -> tuple[float, float]:
        return (along, self.pos) if self.axis == "h" else (self.pos, along)

    def footprint(self, end_extension: float = 0.0):
        half = self.thickness / 2
        if self.axis == "h":
            return box(self.lo - end_extension, self.pos - half, self.hi + end_extension, self.pos + half)
        return box(self.pos - half, self.lo - end_extension, self.pos + half, self.hi + end_extension)


@dataclass
class Plan:
    runs: list[Run]
    outline: Polygon  # filled outer outline of the wall cluster (page points)
    bbox: tuple[float, float, float, float]
    label: str = ""
    closed: bool = True


@dataclass
class WallResult:
    page_number: int
    mode: str  # "fill" | "pair" | "none"
    heavy_width: float | None
    plans: list[Plan]
    bars: list[Bar]
    dropped_clusters: int = 0
    unmatched_tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. wall bars
# ---------------------------------------------------------------------------


def heavy_pen(vs: VectorSet) -> float | None:
    """The heaviest pen width that carries a meaningful share of long lines."""
    if not vs.width_classes:
        return None
    biggest = max(vs.width_classes.values())
    eligible = [w for w, total in vs.width_classes.items() if total >= max(0.15 * biggest, 200)]
    return max(eligible) if eligible else None


def bars_from_fills(vs: VectorSet, ppm: float) -> list[Bar]:
    lo, hi = MIN_T_MM * ppm, MAX_T_MM * ppm
    return [b for b in vs.bars if lo <= b.thickness <= hi and b.length >= MIN_PIECE_MM * ppm]


def _consumed_fraction(intervals: list[tuple[float, float]], lo: float, hi: float) -> float:
    covered = 0.0
    for a, b in intervals:
        covered += max(0.0, overlap(a, b, lo, hi))
    return covered / max(hi - lo, 1e-6)


def _has_line_at(lines: list[Seg], pos: float, tol: float, lo: float, hi: float) -> bool:
    return any(abs(s.pos - pos) <= tol and overlap(s.lo, s.hi, lo, hi) >= 0.5 * (hi - lo) for s in lines)


def _hatched(diagonals: list[Seg], axis: Axis, p0: float, p1: float, lo: float, hi: float) -> bool:
    """Is the band between two parallel lines hatched (short diagonal strokes)? That is
    how a brick leaf is drawn, and what tells a brick-veneer wall from two stud walls
    facing each other across a gap."""
    n = 0
    for s in diagonals:
        mx, my = s.mid
        along, across = (mx, my) if axis == "h" else (my, mx)
        if p0 < across < p1 and lo <= along <= hi:
            n += 1
    return n >= max(3, int((hi - lo) / 15))


def _band_taken(accepted: list[Bar], bar: Bar) -> bool:
    """Does the new bar lie mostly inside the band of a bar already accepted?"""
    for a in accepted:
        if a.axis != bar.axis:
            continue
        across = overlap(bar.pos - bar.thickness / 2, bar.pos + bar.thickness / 2, a.pos - a.thickness / 2, a.pos + a.thickness / 2)
        if across >= 0.5 * min(bar.thickness, a.thickness) and overlap(bar.lo, bar.hi, a.lo, a.hi) >= 0.5 * bar.length:
            return True
    return False


def bars_from_pairs(segs: list[Seg], heavy: float, ppm: float, kinds: tuple[str, ...] = ("stroke", "fill")) -> list[Bar]:
    """Pairs of parallel lines at wall spacing. `kinds` limits the lines used: in fill
    mode only the edges of solid polygons are paired (L-shaped wall fills that are
    not plain rectangles), in pair mode every heavy line."""
    min_t, max_t = MIN_T_MM * ppm, MAX_T_MM * ppm
    min_ov = MIN_PIECE_MM * ppm
    diagonals = [s for s in segs if s.axis is None and 2.0 <= s.length <= 12.0]
    cands = [
        s for s in segs
        if s.axis and not s.dashed and s.kind in kinds
        and s.width >= heavy - 0.05 and s.length >= MIN_LINE_MM * ppm
    ]
    proposals: list[tuple[tuple, Bar, int, int]] = []
    for axis in ("h", "v"):
        lines = sorted((s for s in cands if s.axis == axis), key=lambda s: s.pos)
        for i, a in enumerate(lines):
            for j in range(i + 1, len(lines)):
                b = lines[j]
                d = b.pos - a.pos
                if d > max_t:
                    break
                if d < min_t:
                    continue
                lo, hi = max(a.lo, b.lo), min(a.hi, b.hi)
                if hi - lo < min_ov:
                    continue
                between = [
                    s for s in lines[i + 1 : j]
                    if s.pos > a.pos + 0.3 and s.pos < b.pos - 0.3 and overlap(s.lo, s.hi, lo, hi) >= 0.5 * (hi - lo)
                ]
                if between and not _hatched(diagonals, axis, a.pos, b.pos, lo, hi):
                    continue  # a line inside the band means a separate wall or joinery, unless it is a hatched brick leaf
                tol = 0.15 * d
                left = _has_line_at(lines, a.pos - d, tol, lo, hi)
                right = _has_line_at(lines, b.pos + d, tol, lo, hi)
                left2 = _has_line_at(lines, a.pos - 2 * d, tol, lo, hi)
                right2 = _has_line_at(lines, b.pos + 2 * d, tol, lo, hi)
                if (left and right) or (left and left2) or (right and right2):
                    continue  # evenly spaced repeat: stairs, tiles, hatching
                t_mm = d / ppm
                nominal = nominal_for(t_mm) is not None
                key = (not nominal, -d, -(hi - lo))
                proposals.append((key, Bar(axis, (a.pos + b.pos) / 2, lo, hi, d, "pair"), id(a), id(b)))
    proposals.sort(key=lambda p: p[0])
    consumed: dict[int, list[tuple[float, float]]] = {}
    accepted: list[Bar] = []
    for _, bar, ia, ib in proposals:
        if _consumed_fraction(consumed.get(ia, []), bar.lo, bar.hi) > 0.5:
            continue
        if _consumed_fraction(consumed.get(ib, []), bar.lo, bar.hi) > 0.5:
            continue
        if _band_taken(accepted, bar):
            continue
        accepted.append(bar)
        consumed.setdefault(ia, []).append((bar.lo, bar.hi))
        consumed.setdefault(ib, []).append((bar.lo, bar.hi))
    return [b for b in accepted if nominal_for(b.thickness / ppm) is not None or b.length >= JUNK_PIECE_MM * ppm]


def nominal_for(t_mm: float) -> float | None:
    for n in NOMINAL_MM:
        if abs(t_mm - n) <= NOMINAL_TOL_MM:
            return float(n)
    return None


# ---------------------------------------------------------------------------
# 2. runs and openings
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """What the opening test may look at."""

    light_segments: list[Seg]
    curves: list[tuple[float, float, float, float]]
    tag_lines: list[tuple[Line, Tag]]
    ppm: float


def _gap_box(axis: Axis, pos: float, t: float, lo: float, hi: float, across: float):
    if axis == "h":
        return (lo, pos - across, hi, pos + across)
    return (pos - across, lo, pos + across, hi)


def _window_lines(ev: Evidence, axis: Axis, pos: float, t: float, lo: float, hi: float) -> bool:
    for s in ev.light_segments:
        if s.axis != axis or abs(s.pos - pos) > t / 2 + 0.6:
            continue
        if overlap(s.lo, s.hi, lo, hi) >= 0.5 * (hi - lo):
            return True
    return False


def _arc_near(ev: Evidence, axis: Axis, pos: float, t: float, lo: float, hi: float) -> bool:
    gap = hi - lo
    gx0, gy0, gx1, gy1 = _gap_box(axis, pos, t, lo, hi, gap)
    for cx0, cy0, cx1, cy1 in ev.curves:
        size = max(cx1 - cx0, cy1 - cy0)
        if not (0.35 * gap <= size <= 1.6 * gap):
            continue
        if cx0 <= gx1 and gx0 <= cx1 and cy0 <= gy1 and gy0 <= cy1:
            return True
    return False


def _nearest_tag(ev: Evidence, axis: Axis, pos: float, t: float, lo: float, hi: float) -> tuple[Line, Tag, float] | None:
    reach = TAG_REACH_MM * ev.ppm
    gap = hi - lo
    best = None
    for line, tag in ev.tag_lines:
        cx, cy = (line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2
        along, across = (cx, cy) if axis == "h" else (cy, cx)
        if not (lo - max(0.3 * gap, 4) <= along <= hi + max(0.3 * gap, 4)):
            continue
        d = abs(across - pos)
        if d > reach:
            continue
        if tag.width_mm and abs(tag.width_mm * ev.ppm - gap) > max(0.35 * gap, 6):
            continue  # a tag for a different-sized opening
        if best is None or d < best[2]:
            best = (line, tag, d)
    return best


def _junction_at(cross: list[Bar], axis: Axis, pos: float, t: float, along: float) -> bool:
    """Is there a cross wall meeting this run at coordinate `along`?"""
    for b in cross:
        if abs(b.pos - along) <= b.thickness / 2 + 1.0 and b.lo <= pos + t / 2 + 1.0 and b.hi >= pos - t / 2 - 1.0:
            return True
    return False


def _cross_at_gap(cross: list[Bar], axis: Axis, pos: float, t: float, lo: float, hi: float) -> bool:
    """A cross wall that meets or crosses this wall exactly over the gap: the gap is
    just the drafting break at a T or X junction, not an opening."""
    for b in cross:
        touches = b.lo <= pos + t / 2 + 1.0 and b.hi >= pos - t / 2 - 1.0
        covers = b.pos - b.thickness / 2 - 1.0 <= lo and b.pos + b.thickness / 2 + 1.0 >= hi
        if touches and covers:
            return True
    return False


def build_runs(bars: list[Bar], ev: Evidence) -> list[Run]:
    ppm = ev.ppm
    runs: list[Run] = []
    for axis in ("h", "v"):
        same = sorted((b for b in bars if b.axis == axis), key=lambda b: (b.pos, b.lo))
        cross = [b for b in bars if b.axis != axis]
        # group collinear bars of like thickness
        groups: list[list[Bar]] = []
        for b in same:
            placed = False
            for g in groups:
                if abs(g[0].pos - b.pos) <= 1.0 and abs(g[0].thickness - b.thickness) <= 1.2:
                    g.append(b)
                    placed = True
                    break
            if not placed:
                groups.append([b])
        for g in groups:
            g.sort(key=lambda b: b.lo)
            pos = sum(b.pos * b.length for b in g) / sum(b.length for b in g)
            t = sum(b.thickness * b.length for b in g) / sum(b.length for b in g)
            cur = Run(axis, pos, g[0].lo, g[0].hi, t, [g[0]])
            for b in g[1:]:
                gap_lo, gap_hi = cur.hi, b.lo
                gap = gap_hi - gap_lo
                if gap <= SILENT_GAP_MM * ppm or _cross_at_gap(cross, axis, pos, t, gap_lo, gap_hi):
                    cur.hi = max(cur.hi, b.hi)
                    cur.pieces.append(b)
                    continue
                opening = None
                if gap <= MAX_OPENING_MM * ppm:
                    tag = _nearest_tag(ev, axis, pos, t, gap_lo, gap_hi)
                    if tag is not None:
                        opening = OpeningCandidate(gap_lo, gap_hi, tag[1].kind, tag[1], tag[0].text, "tag", 0.9)
                    elif _window_lines(ev, axis, pos, t, gap_lo, gap_hi):
                        opening = OpeningCandidate(gap_lo, gap_hi, "window", None, "", "window_lines", 0.7)
                    elif _arc_near(ev, axis, pos, t, gap_lo, gap_hi):
                        opening = OpeningCandidate(gap_lo, gap_hi, "door", None, "", "arc", 0.7)
                    elif gap <= UNTAGGED_MAX_OPENING_MM * ppm and not (
                        _junction_at(cross, axis, pos, t, gap_lo) and _junction_at(cross, axis, pos, t, gap_hi)
                    ):
                        opening = OpeningCandidate(gap_lo, gap_hi, "unknown", None, "", "no_junction", 0.4)
                if opening is None:
                    runs.append(cur)
                    cur = Run(axis, pos, b.lo, b.hi, t, [b])
                else:
                    cur.openings.append(opening)
                    cur.hi = max(cur.hi, b.hi)
                    cur.pieces.append(b)
            runs.append(cur)
    return runs



def _light_runs_in_band(ev: Evidence, axis: Axis, pos: float, t: float, strict: bool) -> list[tuple[float, float]]:
    """Intervals along the axis where at least two distinct light (non-wall) lines run
    inside the wall band - a window has an inner and an outer face line, a stray
    deck edge or shelf has one. `strict` keeps clear of the wall faces so the faces
    themselves do not count."""
    margin = t / 2 - 0.4 if strict else t / 2 + 0.6
    segs = [s for s in ev.light_segments if s.axis == axis and abs(s.pos - pos) <= margin and s.length >= 3]
    events: list[tuple[float, int, float]] = []
    for s in segs:
        events.append((s.lo, 1, s.pos))
        events.append((s.hi, -1, s.pos))
    events.sort(key=lambda e: (e[0], e[1]))
    open_pos: list[float] = []
    out: list[list[float]] = []
    start: float | None = None
    for x, delta, p in events:
        if delta == 1:
            open_pos.append(p)
        else:
            open_pos.remove(p)
        distinct = len({round(q, 1) for q in open_pos}) >= 2 and (max(open_pos) - min(open_pos) >= 0.8)
        if distinct and start is None:
            start = x
        elif not distinct and start is not None:
            if out and start <= out[-1][1] + 1.0:
                out[-1][1] = max(out[-1][1], x)
            elif x - start >= 1.0:
                out.append([start, x])
            start = None
    return [(a, b) for a, b in out]


def _light_extent_from_end(ev: Evidence, run: Run, side: str) -> float | None:
    """How far light lines in the wall band continue past a run end (window at the
    end of a wall). Returns the far coordinate, or None."""
    end = run.lo if side == "lo" else run.hi
    for lo, hi in _light_runs_in_band(ev, run.axis, run.pos, run.thickness, strict=False):
        if side == "hi" and lo <= end + 2.0 and hi > end + 2.0:
            return hi
        if side == "lo" and hi >= end - 2.0 and lo < end - 2.0:
            return lo
    return None


def _cross_target(runs: list[Run], run: Run, side: str, ppm: float) -> Run | None:
    """Nearest cross run whose centre-line lies beyond this end within an opening's
    width and which passes across this run's band."""
    end = run.lo if side == "lo" else run.hi
    best = None
    for c in runs:
        if c.axis == run.axis:
            continue
        if not (c.lo - c.thickness / 2 - 1.0 <= run.pos <= c.hi + c.thickness / 2 + 1.0):
            continue
        d = (c.pos - end) if side == "hi" else (end - c.pos)
        if SILENT_GAP_MM * ppm < d <= MAX_OPENING_MM * ppm and (best is None or d < best[0]):
            best = (d, c)
    return best[1] if best else None


def extend_run_ends(runs: list[Run], ev: Evidence) -> None:
    """An opening at the end of a wall (door beside a corner, window up to a return)
    leaves the run short. Stretch it when there is evidence of an opening across the
    gap: to the cross wall it meets, or as far as the window lines go."""
    ppm = ev.ppm
    for r in runs:
        for side in ("lo", "hi"):
            end = r.lo if side == "lo" else r.hi
            target = _cross_target(runs, r, side, ppm)
            far = _light_extent_from_end(ev, r, side)
            new_end = None
            opening = None
            if target is not None:
                face = target.pos - target.thickness / 2 if side == "hi" else target.pos + target.thickness / 2
                g_lo, g_hi = (end, face) if side == "hi" else (face, end)
                continues = any(o is not r and o.axis == r.axis and abs(o.pos - r.pos) <= 1.0 and overlap(o.lo, o.hi, g_lo, g_hi) > 0 for o in runs)
                if continues:
                    target = None  # that stretch of wall is already a run of its own
                elif g_hi - g_lo >= MIN_OPENING_MM * ppm:
                    tag = _nearest_tag(ev, r.axis, r.pos, r.thickness, g_lo, g_hi)
                    if tag is not None:
                        opening = OpeningCandidate(g_lo, g_hi, tag[1].kind, tag[1], tag[0].text, "tag", 0.9)
                    elif far is not None and (abs(far - face) <= 3.0 or (far > face if side == "hi" else far < face)):
                        opening = OpeningCandidate(g_lo, g_hi, "window", None, "", "window_lines", 0.7)
                    elif _arc_near(ev, r.axis, r.pos, r.thickness, g_lo, g_hi):
                        opening = OpeningCandidate(g_lo, g_hi, "door", None, "", "arc", 0.7)
                    if opening is not None:
                        new_end = target.pos
            if opening is None and far is not None:
                g_lo, g_hi = (end, far) if side == "hi" else (far, end)
                if MIN_OPENING_MM * ppm <= g_hi - g_lo <= MAX_OPENING_MM * ppm:
                    tag = _nearest_tag(ev, r.axis, r.pos, r.thickness, g_lo, g_hi)
                    kind = tag[1].kind if tag else "window"
                    opening = OpeningCandidate(g_lo, g_hi, kind, tag[1] if tag else None, tag[0].text if tag else "",
                                               "tag" if tag else "window_lines", 0.8 if tag else 0.6)
                    new_end = far
            if opening is not None and new_end is not None:
                r.openings.append(opening)
                if side == "hi":
                    r.hi = max(r.hi, new_end)
                else:
                    r.lo = min(r.lo, new_end)
        r.openings.sort(key=lambda o: o.lo)


def glazed_walls(runs: list[Run], ev: Evidence) -> list[Run]:
    """A side of a room that is all glass has no wall bar at all: a wall ends on a
    line where window lines run across to another wall (which either ends on the
    same line or passes through it). Make that a run whose whole length is one
    opening, so the outline closes and the opening is recorded."""
    ppm = ev.ppm
    made: list[Run] = []
    for axis in ("h", "v"):
        same = [r for r in runs if r.axis == axis]
        other: Axis = "v" if axis == "h" else "h"
        for r1 in same:
            for side in ("lo", "hi"):
                e = r1.lo if side == "lo" else r1.hi
                for r2 in same:
                    if r2 is r1:
                        continue
                    # the two walls meet the glass line at a corner each: their ends may differ by a wall thickness
                    corner_tol = max(r1.thickness, r2.thickness) + 1.5
                    ends_here = abs((r2.lo if side == "lo" else r2.hi) - e) <= corner_tol
                    passes = r2.lo + 1.5 < e < r2.hi - 1.5
                    if not (ends_here or passes):
                        continue
                    span = abs(r2.pos - r1.pos)
                    if not (MIN_OPENING_MM * ppm <= span <= MAX_OPENING_MM * ppm):
                        continue
                    t = (r1.thickness + r2.thickness) / 2
                    lo, hi = min(r1.pos, r2.pos), max(r1.pos, r2.pos)
                    # another wall of this axis in between would be the real neighbour
                    if any(o is not r1 and o is not r2 and o.axis == axis and lo < o.pos < hi and o.lo - 1.5 <= e <= o.hi + 1.5 for o in same):
                        continue
                    # already walled, or the end simply butts into a cross wall?
                    if any(o.axis == other and abs(o.pos - e) <= max(t, o.thickness) + 1.0 and overlap(o.lo, o.hi, lo, hi) > 0.3 * span for o in runs + made):
                        continue
                    if any(o.axis == other and abs(o.pos - e) <= max(t, o.thickness) + 1.5
                           and o.lo - o.thickness - 1 <= r1.pos <= o.hi + o.thickness + 1 for o in runs):
                        continue
                    covered = sum(max(0.0, overlap(a, b, lo, hi)) for a, b in _light_runs_in_band(ev, other, e, t, strict=False))
                    if covered < 0.5 * span:
                        continue
                    run = Run(other, e, lo, hi, t, [])
                    run.openings.append(OpeningCandidate(lo, hi, "window", None, "", "window_lines", 0.6))
                    made.append(run)
    return made


def openings_within_runs(runs: list[Run], ev: Evidence) -> None:
    """Windows drawn inside a wall whose face lines run on past them (brick veneer:
    only the brick skin and the light window lines break)."""
    ppm = ev.ppm
    for r in runs:
        for lo, hi in _light_runs_in_band(ev, r.axis, r.pos, r.thickness, strict=True):
            lo, hi = max(lo, r.lo), min(hi, r.hi)
            if not (MIN_OPENING_MM * ppm <= hi - lo <= MAX_OPENING_MM * ppm):
                continue
            if any(overlap(o.lo, o.hi, lo, hi) > 0 for o in r.openings):
                continue
            r.openings.append(OpeningCandidate(lo, hi, "window", None, "", "window_lines", 0.6))
        r.openings.sort(key=lambda o: o.lo)


def attach_tags(runs: list[Run], ev: Evidence) -> None:
    """Give every opening the nearest unused tag; a tag with a width sets the kind."""
    cands: list[tuple[float, OpeningCandidate, Run, Line, Tag]] = []
    reach = TAG_REACH_MM * ev.ppm
    for r in runs:
        for o in r.openings:
            gap = o.width_pt
            for line, tag in ev.tag_lines:
                cx, cy = (line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2
                along, across = (cx, cy) if r.axis == "h" else (cy, cx)
                if not (o.lo - max(0.3 * gap, 4) <= along <= o.hi + max(0.3 * gap, 4)):
                    continue
                d = abs(across - r.pos)
                if d > reach:
                    continue
                if tag.width_mm and abs(tag.width_mm * ev.ppm - gap) > max(0.35 * gap, 6):
                    continue
                # prefer text running along the wall, as the architect writes it
                penalty = 0.0 if (line.vertical == (r.axis == "v")) else reach * 0.5
                cands.append((d + penalty, o, r, line, tag))
    cands.sort(key=lambda c: c[0])
    used_lines: set[int] = set()
    done: set[int] = set()
    for _, o, r, line, tag in cands:
        if id(line) in used_lines or id(o) in done:
            continue
        used_lines.add(id(line))
        done.add(id(o))
        o.tag, o.tag_text = tag, line.text
        if not tag.is_id_only or o.kind == "unknown":
            o.kind = tag.kind
        o.evidence = "tag"
        o.confidence = max(o.confidence, 0.9 if tag.width_mm else 0.75)


# ---------------------------------------------------------------------------
# 3. corners
# ---------------------------------------------------------------------------


def snap_corners(runs: list[Run]) -> None:
    for r in runs:
        cross = [c for c in runs if c.axis != r.axis]
        for end in ("lo", "hi"):
            e = getattr(r, end)
            best = None
            for c in cross:
                if not (c.lo - c.thickness / 2 - 1.0 <= r.pos <= c.hi + c.thickness / 2 + 1.0):
                    continue
                d = abs(c.pos - e)
                # among the cross walls within reach prefer the longest: a real wall over a stair rail or nib
                if d <= c.thickness / 2 + 1.5 * r.thickness + 1.5 and (best is None or c.length > best[0]):
                    best = (c.length, c.pos)
            if best is not None:
                new = best[1]
                shortens = (new - e) if end == "lo" else (e - new)
                jamb = min((o.lo for o in r.openings), default=r.hi) if end == "lo" else max((o.hi for o in r.openings), default=r.lo)
                # never pull an end back past an opening, and only a little past the drawn end
                if shortens <= 0 or (shortens <= r.thickness / 2 + 1.0 and (new <= jamb if end == "lo" else new >= jamb)):
                    setattr(r, end, new)
        if r.hi < r.lo:
            r.lo, r.hi = r.hi, r.lo
        for o in r.openings:
            o.lo, o.hi = max(o.lo, r.lo), min(o.hi, r.hi)


# ---------------------------------------------------------------------------
# 4. plans and external walls
# ---------------------------------------------------------------------------


def _polygons(geom) -> list[Polygon]:
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in geom.geoms if g.geom_type == "Polygon"]


def cluster_plans(runs: list[Run], ppm: float) -> tuple[list[Plan], int]:
    if not runs:
        return [], 0
    foot = [r.footprint(end_extension=r.thickness / 2) for r in runs]
    union = unary_union(foot).buffer(0.5).buffer(-0.5)  # close hairline gaps between footprints
    plans: list[Plan] = []
    dropped = 0
    min_size = PLAN_MIN_SIZE_MM * ppm
    for poly in _polygons(union):
        members = [r for r, f in zip(runs, foot) if f.intersects(poly)]
        x0, y0, x1, y1 = poly.bounds
        if len(members) < PLAN_MIN_WALLS or (x1 - x0) < min_size or (y1 - y0) < min_size:
            dropped += 1
            continue
        outline = Polygon(poly.exterior)
        plans.append(Plan(members, outline, (x0, y0, x1, y1)))
    plans.sort(key=lambda p: p.bbox[0])
    return plans, dropped


def _outside_profile(outline: Polygon, r: Run, step: float = 6.0) -> list[tuple[float, bool]]:
    """Along the run, whether open air lies on either face at that point."""
    off = r.thickness / 2 + 2.0
    n = max(3, int(r.length / step))
    out = []
    for k in range(n):
        along = r.lo + (k + 0.5) * r.length / n
        x, y = r.point(along)
        if r.axis == "h":
            pts = [(x, y - off), (x, y + off)]
        else:
            pts = [(x - off, y), (x + off, y)]
        out.append((along, any(not outline.contains(Point(px, py)) for px, py in pts)))
    return out


def split_mixed_runs(plan: Plan) -> None:
    """A wall that is external along part of its length and internal along the rest
    (the return beside a deck, say) changes status only where a cross wall meets it.
    Split such runs at those junctions so each piece gets one honest answer."""
    new_runs: list[Run] = []
    for r in plan.runs:
        profile = _outside_profile(plan.outline, r)
        flags = {f for _, f in profile}
        if len(flags) < 2:
            new_runs.append(r)
            continue
        cuts = sorted({
            c.pos for c in plan.runs
            if c.axis != r.axis and r.lo + 1 < c.pos < r.hi - 1
            and c.lo - c.thickness / 2 - 1.0 <= r.pos <= c.hi + c.thickness / 2 + 1.0
        })
        # keep only cuts where the status really changes
        def status(a: float, b: float) -> bool:
            inside = [f for along, f in profile if a <= along <= b]
            return sum(inside) >= len(inside) / 2 if inside else False
        bounds = [r.lo] + cuts + [r.hi]
        pieces: list[tuple[float, float]] = []
        for a, b in zip(bounds, bounds[1:]):
            if pieces and status(*pieces[-1]) == status(a, b):
                pieces[-1] = (pieces[-1][0], b)
            else:
                pieces.append((a, b))
        if len(pieces) < 2:
            new_runs.append(r)
            continue
        for a, b in pieces:
            part = Run(r.axis, r.pos, a, b, r.thickness, r.pieces, id=r.id)
            part.openings = [o for o in r.openings if a <= (o.lo + o.hi) / 2 <= b]
            new_runs.append(part)
    plan.runs = new_runs


def classify_external(plan: Plan) -> None:
    outline = plan.outline
    split_mixed_runs(plan)
    for r in plan.runs:
        off = r.thickness / 2 + 2.0
        n = 7
        outside = [0, 0]
        for k in range(n):
            along = r.lo + (k + 0.5) * r.length / n
            for side, sign in enumerate((-1, 1)):
                x, y = r.point(along)
                if r.axis == "h":
                    y += sign * off
                else:
                    x += sign * off
                if not outline.contains(Point(x, y)):
                    outside[side] += 1
        r.external = max(outside) >= math.ceil(n * 0.5)
    ext = [r for r in plan.runs if r.external]
    if ext:
        ring = unary_union([r.footprint(end_extension=r.thickness / 2).buffer(0.5) for r in ext])
        plan.closed = any(len(p.interiors) > 0 for p in _polygons(ring))
    else:
        plan.closed = False


# ---------------------------------------------------------------------------
# 5. labels
# ---------------------------------------------------------------------------


def underlined_titles(scan: PageScan, segs: list[Seg]) -> list[Line]:
    out = []
    for ln in scan.lines:
        if ln.vertical or ln.size < 10 or len(ln.text) < 3:
            continue
        x0, y0, x1, y1 = ln.bbox
        for s in segs:
            if s.axis == "h" and y0 - 4.5 <= s.pos <= y0 + 1.5 and 0.8 * (x1 - x0) <= s.length <= 1.6 * (x1 - x0) \
                    and overlap(s.lo, s.hi, x0, x1) >= 0.8 * (x1 - x0):
                out.append(ln)
                break
    return out


def label_plans(plans: list[Plan], titles: list[Line], fallback: str) -> None:
    used: set[int] = set()
    for p in plans:
        best = None
        for i, ln in enumerate(titles):
            if i in used:
                continue
            cx, cy = (ln.bbox[0] + ln.bbox[2]) / 2, (ln.bbox[1] + ln.bbox[3]) / 2
            dx = max(0.0, p.bbox[0] - cx, cx - p.bbox[2])
            dy = max(0.0, p.bbox[1] - cy, cy - p.bbox[3])
            d = math.hypot(dx, dy)
            if d <= 250 and (best is None or d < best[0]):
                best = (d, i)
        if best is not None:
            used.add(best[1])
            p.label = titles[best[1]].text
        else:
            p.label = fallback


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def detect_walls(
    scan: PageScan, vs: VectorSet, scale_denominator: float | None,
    dimension_boxes: list[tuple[float, float, float, float]] | None = None,
) -> WallResult:
    """`dimension_boxes` are the text boxes of numbers already identified as
    dimensions, so a `1200` on a dimension line is not mistaken for a door tag."""
    scale = scale_denominator or 100.0
    ppm = pts_per_mm(scale)
    notes: list[str] = []
    if scale_denominator is None:
        notes.append("no scale known for this page; wall thresholds assume 1:100")

    fill_bars = bars_from_fills(vs, ppm)
    heavy = heavy_pen(vs)
    if sum(b.length for b in fill_bars) >= FILL_MODE_MIN_LENGTH_MM * ppm:
        mode, bars = "fill", list(fill_bars)
        # solid wall shapes that are not plain rectangles (returns, nibs) come from their edges
        for b in bars_from_pairs(vs.segments, 0.0, ppm, kinds=("fill",)):
            if not _band_taken(bars, b):
                bars.append(b)
    elif heavy is not None:
        mode, bars = "pair", bars_from_pairs(vs.segments, heavy, ppm)
    else:
        mode, bars = "none", []

    tag_lines = []
    dim_centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in (dimension_boxes or [])]
    for ln in scan.lines:
        tag = parse_tag(ln.text)
        if tag is None:
            continue
        x0, y0, x1, y1 = ln.bbox
        if any(x0 - 0.5 <= cx <= x1 + 0.5 and y0 - 0.5 <= cy <= y1 + 0.5 for cx, cy in dim_centres):
            continue  # it is a dimension string
        tag_lines.append((ln, tag))
    light = [s for s in vs.segments if s.axis and (mode == "fill" or heavy is None or s.width < heavy - 0.05)]
    ev = Evidence(light, vs.curves, tag_lines, ppm)

    runs = build_runs(bars, ev)
    extend_run_ends(runs, ev)
    runs += glazed_walls(runs, ev)
    snap_corners(runs)
    plans, dropped = cluster_plans(runs, ppm)
    for plan in plans:
        classify_external(plan)
        openings_within_runs([r for r in plan.runs if r.external], ev)
        attach_tags(plan.runs, ev)
        for r in plan.runs:
            r.nominal_mm = nominal_for(r.thickness / ppm)
    label_plans(plans, underlined_titles(scan, vs.segments), scan.title)

    used = {o.tag_text for p in plans for r in p.runs for o in r.openings if o.tag_text}
    unmatched = sorted({ln.text for ln, tag in tag_lines if ln.text not in used and not tag.is_id_only}
                       | {ln.text for ln in scan.lines if looks_like_tag(ln.text) and parse_tag(ln.text) is None})
    return WallResult(scan.number, mode, heavy, plans, bars, dropped, unmatched, notes)
