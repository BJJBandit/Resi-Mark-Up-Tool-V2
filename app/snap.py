"""Snap lintels onto openings, and bearers / beams / footings onto walls, in a vector
(CAD-exported) drawing.

Claude places mark-ups by reading a page image, so a lintel can land a little off
its opening. Most architectural PDFs are CAD exports where every wall and opening
is an exact vector line. In plan, each opening is bounded by two short **jamb**
lines drawn across the wall thickness. This module finds the pair of jambs that
best matches where Claude put the lintel and moves the lintel onto them: along
the wall centreline, from jamb to jamb, plus a bearing of about one wall
thickness each side (matching the engineer's own mark-ups).

If no convincing opening is found nearby (or the drawing is a raster scan with no
vectors) the lintel is left where Claude placed it and reported, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf


@dataclass
class Seg:
    a: float  # position along the axis (start)
    b: float  # position along the axis (end), a <= b
    c: float  # fixed coordinate across the axis


class Geometry:
    """Axis-aligned, neutral-coloured line work of the architectural drawing, in
    displayed page coordinates (the same space the page image Claude saw uses)."""

    def __init__(self, page: pymupdf.Page) -> None:
        self.h: list[Seg] = []  # horizontal: a..b along x at y=c
        self.v: list[Seg] = []  # vertical:   a..b along y at x=c
        m = page.rotation_matrix
        for g in page.get_drawings():
            col = g.get("color")
            # Architectural line work is grey/black; skip coloured mark-up lines.
            if col and max(col) - min(col) > 0.15:
                continue
            for it in g["items"]:
                if it[0] == "l":
                    self._add(it[1] * m, it[2] * m)
                elif it[0] == "re":
                    r = it[1] * m
                    for p, q in ((r.tl, r.tr), (r.bl, r.br), (r.tl, r.bl), (r.tr, r.br)):
                        self._add(p, q)

    def _add(self, p: pymupdf.Point, q: pymupdf.Point) -> None:
        if abs(p.y - q.y) < 0.3 and abs(p.x - q.x) > 0.3:
            self.h.append(Seg(min(p.x, q.x), max(p.x, q.x), (p.y + q.y) / 2))
        elif abs(p.x - q.x) < 0.3 and abs(p.y - q.y) > 0.3:
            self.v.append(Seg(min(p.y, q.y), max(p.y, q.y), (p.x + q.x) / 2))

    @property
    def usable(self) -> bool:
        return len(self.h) + len(self.v) > 200  # a raster scan has (almost) none

    _walls: tuple[list[tuple[float, float, float, float]], list[tuple[float, float, float, float]]] | None = None

    @property
    def walls(self) -> tuple[list[tuple[float, float, float, float]], list[tuple[float, float, float, float]]]:
        """Wall centreline pieces (c, lo, hi, thickness) for horizontal and vertical
        walls: pairs of parallel face lines 1.5-12 pt apart that run alongside each
        other for at least 8 pt."""
        if self._walls is None:
            self._walls = (_wall_pieces(self.h), _wall_pieces(self.v))
        return self._walls


def _wall_pieces(segs: list[Seg], min_len: float = 8.0) -> list[tuple[float, float, float, float]]:
    long = sorted((s for s in segs if s.b - s.a >= min_len), key=lambda s: s.c)
    out = []
    for i, s1 in enumerate(long):
        for s2 in long[i + 1:]:
            t = s2.c - s1.c
            if t > 12:
                break
            if t < 1.5:
                continue
            lo, hi = max(s1.a, s2.a), min(s1.b, s2.b)
            if hi - lo >= min_len:
                out.append(((s1.c + s2.c) / 2, lo, hi, t))
    return out


def _coverage(pieces: list[tuple[float, float, float, float]], a0: float, a1: float) -> float:
    """Length of [a0, a1] covered by the union of the pieces (gaps <= 3 pt bridged,
    so a wall interrupted by a jamb or junction still counts as one wall)."""
    ivs = sorted((max(lo, a0), min(hi, a1)) for _, lo, hi, _ in pieces if min(hi, a1) > max(lo, a0))
    total, cur = 0.0, None
    for lo, hi in ivs:
        if cur is None or lo > cur[1] + 3:
            if cur:
                total += cur[1] - cur[0]
            cur = [lo, hi]
        else:
            cur[1] = max(cur[1], hi)
    if cur:
        total += cur[1] - cur[0]
    return total


def snap_to_wall(geo: Geometry, p: pymupdf.Point, q: pymupdf.Point) -> tuple[pymupdf.Point, pymupdf.Point] | None:
    """Move a bearer / beam / footing onto the wall it runs along, if there is one.

    Only snaps when a wall centreline within reach covers at least half the member's
    length; bearers spanning mid-room (no wall under them) return None and stay put.
    Each end then snaps to a crossing wall's centreline if one is close."""
    horizontal = abs(q.x - p.x) >= abs(q.y - p.y)
    if horizontal:
        a0, a1 = sorted((p.x, q.x))
        c0 = (p.y + q.y) / 2
    else:
        a0, a1 = sorted((p.y, q.y))
        c0 = (p.x + q.x) / 2
    span = a1 - a0
    if span < 8:
        return None
    along, across = (geo.walls[0], geo.walls[1]) if horizontal else (geo.walls[1], geo.walls[0])

    across_tol = 8.0
    near = [w for w in along if abs(w[0] - c0) <= across_tol and w[2] > a0 and w[1] < a1]
    if not near:
        return None
    best = None
    for c in sorted({round(w[0] * 2) / 2 for w in near}):
        group = [w for w in near if abs(w[0] - c) <= 1.0]
        cover = _coverage(group, a0, a1) / span
        centre = sum(w[0] for w in group) / len(group)
        score = cover - 0.02 * abs(centre - c0)
        if best is None or score > best[0]:
            best = (score, cover, centre)
    if best is None or best[1] < 0.5:
        return None
    c = best[2]

    # Ends: snap to the centreline of a crossing wall, if one is close.
    end_tol = min(12.0, max(6.0, 0.3 * span))

    def snap_end(v: float) -> float:
        # At an L-corner the crossing wall's centreline stops at our wall's inner face,
        # so allow a wall thickness of slack when checking that it reaches our wall.
        cands = [w[0] for w in across if w[1] - w[3] - 1 <= c <= w[2] + w[3] + 1 and abs(w[0] - v) <= end_tol]
        return min(cands, key=lambda x: abs(x - v)) if cands else v

    s, e = snap_end(a0), snap_end(a1)
    if e - s < 0.5 * span:  # ends collapsed onto the same wall: keep Claude's
        s, e = a0, a1
    if horizontal:
        return pymupdf.Point(s, c), pymupdf.Point(e, c)
    return pymupdf.Point(c, s), pymupdf.Point(c, e)


def snap_span(geo: Geometry, p: pymupdf.Point, q: pymupdf.Point) -> tuple[pymupdf.Point, pymupdf.Point] | None:
    """Return the snapped lintel end points (displayed page coords), or None."""
    horizontal = abs(q.x - p.x) >= abs(q.y - p.y)
    if horizontal:
        a0, a1 = sorted((p.x, q.x))
        c0 = (p.y + q.y) / 2
        jambs_src = geo.v  # jambs of a horizontal wall are vertical
    else:
        a0, a1 = sorted((p.y, q.y))
        c0 = (p.x + q.x) / 2
        jambs_src = geo.h
    span = a1 - a0
    if span < 3:
        return None

    across_tol = max(14.0, 0.25 * span)  # how far off the wall Claude may be
    along_tol = max(12.0, 0.35 * span)   # how far off each end may be

    # Jambs: short lines across the wall, near the lintel's line and span.
    jambs = [
        s for s in jambs_src
        if 1.0 <= s.b - s.a <= 22.0
        and a0 - along_tol <= s.c <= a1 + along_tol
        and s.a - across_tol <= c0 <= s.b + across_tol
    ]
    if len(jambs) < 2:
        return None
    faces = geo.h if horizontal else geo.v

    def is_jamb(j: Seg, outward: int) -> bool:
        """A real opening jamb has both wall faces running away from it on the
        outside of the opening (tiles, joinery and dimension ticks don't)."""
        for end in (j.a, j.b):
            if not any(
                abs(f.c - end) <= 0.8
                and (f.a <= j.c - 2 and f.b >= j.c - 0.8 if outward < 0 else f.a <= j.c + 0.8 and f.b >= j.c + 2)
                for f in faces
            ):
                return False
        return True

    lefts = [j for j in jambs if is_jamb(j, -1)]
    rights = [j for j in jambs if is_jamb(j, +1)]

    cands: list[tuple[float, float, float, float, float]] = []
    for left in lefts:
        for right in rights:
            if right.c <= left.c:
                continue
            width = right.c - left.c
            if width < 0.5 * span or width > 1.6 * span:
                continue
            # Both jambs must cross the same wall (their extents overlap).
            lo, hi = max(left.a, right.a), min(left.b, right.b)
            if hi - lo < 0.5 * min(left.b - left.a, right.b - right.a):
                continue
            e_left, e_right = abs(left.c - a0), abs(right.c - a1)
            if e_left > along_tol or e_right > along_tol:
                continue
            centre = (lo + hi) / 2
            e_across = abs(centre - c0)
            if e_across > across_tol:
                continue
            score = e_left + e_right + 0.5 * e_across
            cands.append((score, left.c, right.c, lo, hi))
    if not cands:
        return None
    cands.sort()
    best = cands[0]
    # Ambiguous: a clearly different opening fits about as well -> don't guess.
    for other in cands[1:]:
        different = abs(other[1] - best[1]) > 3 or abs(other[2] - best[2]) > 3
        if different and other[0] <= best[0] * 1.25 + 2:
            return None
        if different:
            break

    _, x_left, x_right, lo, hi = best
    centre = (lo + hi) / 2
    bearing = min(hi - lo, 0.1 * (x_right - x_left))  # ~ one wall thickness
    s, e = x_left - bearing, x_right + bearing
    if horizontal:
        return pymupdf.Point(s, centre), pymupdf.Point(e, centre)
    return pymupdf.Point(centre, s), pymupdf.Point(centre, e)


FOOTING_TAGS = {"EB", "SF", "IB"}  # drawn on the wall centreline
BEAM_TAGS = {"B", "VB", "RB", "LD"}  # bearers / beams / ledgers
SIDE_OFFSET = 3.0  # pt: a bearer beside a footing line is drawn this far off it


def snap_members(geo: Geometry, members: list[tuple[str, pymupdf.Point, pymupdf.Point]]):
    """Snap a sheet's footings, bearers and beams onto walls together.

    members: (tag prefix, p, q). Returns a list of (p, q) or None per member.
    Footings go on the wall centreline. A bearer / beam also goes on the centreline,
    unless a footing on this sheet already occupies it, in which case it is drawn
    SIDE_OFFSET beside it on the side Claude placed it (the engineer's house style,
    so both lines stay visible)."""
    out: list = [None] * len(members)
    taken: list[tuple[bool, float, float, float]] = []  # horizontal?, c, lo, hi of footing lines
    order = sorted(range(len(members)), key=lambda i: members[i][0] not in FOOTING_TAGS)
    for i in order:
        tag, p, q = members[i]
        r = snap_to_wall(geo, p, q)
        if r is None:
            continue
        horizontal = abs(r[1].x - r[0].x) >= abs(r[1].y - r[0].y)
        c = r[0].y if horizontal else r[0].x
        lo, hi = sorted((r[0].x, r[1].x) if horizontal else (r[0].y, r[1].y))
        if tag in FOOTING_TAGS:
            taken.append((horizontal, c, lo, hi))
        else:
            clash = any(
                h == horizontal and abs(tc - c) < 1.0 and min(thi, hi) - max(tlo, lo) > 0.3 * (hi - lo)
                for h, tc, tlo, thi in taken
            )
            if clash:
                c0 = (p.y + q.y) / 2 if horizontal else (p.x + q.x) / 2
                c = c + (SIDE_OFFSET if c0 >= c else -SIDE_OFFSET)
                r = (pymupdf.Point(r[0].x, c), pymupdf.Point(r[1].x, c)) if horizontal else \
                    (pymupdf.Point(c, r[0].y), pymupdf.Point(c, r[1].y))
        out[i] = r
    return out


# ---------------------------------------------------------------------------
# Bracing walls
# ---------------------------------------------------------------------------

import math  # noqa: E402
import re  # noqa: E402

STANDARD_SCALES = {20, 25, 50, 75, 100, 125, 150, 200, 250, 500}


def drawing_scale(page: pymupdf.Page, geo: Geometry | None = None) -> tuple[int | None, str]:
    """Plan scale: printed on the sheet ('1:100'), else elsewhere in the set, else
    inferred from the wall thickness (most walls on a house plan are 90 mm studs).
    Returns (scale, how) or (None, reason)."""
    def printed(text: str) -> list[int]:
        return [int(m) for m in re.findall(r"\b1\s*:\s*(\d{2,3})\b", text) if int(m) in STANDARD_SCALES]

    found = printed(page.get_text("text"))
    if found:
        return max(set(found), key=found.count), "printed on the sheet"
    found = [x for pg in page.parent for x in printed(pg.get_text("text"))]
    if found:
        return max(set(found), key=found.count), "printed in the drawing set"
    if geo is not None:
        ts = [round(w[3], 1) for w in geo.walls[0] + geo.walls[1] if 1.5 <= w[3] <= 8]
        if ts:
            t = max(set(ts), key=ts.count)
            est = 90 / (t * 25.4 / 72)  # 90 mm stud wall drawn t pt thick
            best = min((50, 100, 200), key=lambda s: abs(math.log(est / s)))
            if abs(math.log(est / best)) < 0.25:
                return best, "inferred from wall thickness"
    return None, "scale not found"


def mm_to_pt(mm: float, scale: int) -> float:
    return mm / scale * 72 / 25.4


def bracing_length_mm(tag: str) -> float | None:
    """'h(b) - 900' -> 900; 'd - 2400' -> 2400."""
    m = re.search(r"(\d{3,4})\s*$", (tag or "").strip())
    return float(m.group(1)) if m else None


def _find_wall(geo: Geometry, horizontal: bool, a0: float, a1: float, c0: float, across_tol: float = 8.0):
    """(centre, thickness) of the wall running along [a0, a1] near c0, or None."""
    along = geo.walls[0] if horizontal else geo.walls[1]
    near = [w for w in along if abs(w[0] - c0) <= across_tol and w[2] > a0 and w[1] < a1]
    best = None
    for c in sorted({round(w[0] * 2) / 2 for w in near}):
        group = [w for w in near if abs(w[0] - c) <= 1.0]
        cover = _coverage(group, a0, a1) / max(a1 - a0, 1e-6)
        centre = sum(w[0] for w in group) / len(group)
        thick = sum(w[3] for w in group) / len(group)
        score = cover - 0.02 * abs(centre - c0)
        if best is None or score > best[0]:
            best = (score, cover, centre, thick)
    if best is None or best[1] < 0.5:
        return None
    return best[2], best[3]


def _run(geo: Geometry, horizontal: bool, c: float, at: float) -> tuple[float, float] | None:
    """Extent of the continuous wall (gaps <= 3 pt bridged) on centreline c through 'at'."""
    along = geo.walls[0] if horizontal else geo.walls[1]
    ivs = sorted((lo, hi) for wc, lo, hi, _ in along if abs(wc - c) <= 1.0)
    merged: list[list[float]] = []
    for lo, hi in ivs:
        if merged and lo <= merged[-1][1] + 3:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    if not merged:
        return None
    best = min(merged, key=lambda iv: 0 if iv[0] <= at <= iv[1] else min(abs(at - iv[0]), abs(at - iv[1])))
    return best[0], best[1]


def _solid(geo: Geometry, horizontal: bool, c: float, thick: float, lo: float, hi: float) -> list[tuple[float, float]]:
    """Stretches of [lo, hi] that are solid wall: both faces drawn and no window
    glazing line between them (doors show as gaps in the faces)."""
    faces = geo.h if horizontal else geo.v
    f1 = [s for s in faces if abs(s.c - (c - thick / 2)) <= 0.8 and s.b > lo and s.a < hi]
    f2 = [s for s in faces if abs(s.c - (c + thick / 2)) <= 0.8 and s.b > lo and s.a < hi]
    mid = [s for s in faces if c - thick / 2 + 0.4 < s.c < c + thick / 2 - 0.4 and s.b > lo and s.a < hi]
    cov = lambda segs, x: any(s.a - 0.3 <= x <= s.b + 0.3 for s in segs)  # noqa: E731
    step = 0.5
    xs = [lo + i * step for i in range(int((hi - lo) / step) + 1)]
    # Cavity / framing lines drawn along the whole wall are construction, not windows.
    if mid and sum(cov(mid, x) for x in xs) > 0.7 * len(xs):
        mid = []
    out: list[tuple[float, float]] = []
    start = None
    for x in xs:
        ok = cov(f1, x) and cov(f2, x) and not cov(mid, x)
        if ok and start is None:
            start = x
        if not ok and start is not None:
            out.append((start, x - step))
            start = None
    if start is not None:
        out.append((start, xs[-1]))
    return out


def snap_bracing(geo: Geometry, p: pymupdf.Point, q: pymupdf.Point, length_pt: float | None):
    """Place a bracing panel on its wall, house style: SIDE_OFFSET beside the wall
    centreline on the side Claude drew it, at its scaled length, and slid along the
    wall off any window or door. Returns (p, q, note) or None if no wall is found."""
    horizontal = abs(q.x - p.x) >= abs(q.y - p.y)
    a0, a1 = sorted((p.x, q.x) if horizontal else (p.y, q.y))
    c0 = (p.y + q.y) / 2 if horizontal else (p.x + q.x) / 2
    drawn = a1 - a0
    if drawn < 3:
        return None
    # Look for the wall along a slightly longer stretch: panels are short.
    pad = max(0.0, (25 - drawn) / 2)
    # Panels are drawn ~3 pt off the centreline, so allow for that plus Claude's error.
    wall = _find_wall(geo, horizontal, a0 - pad, a1 + pad, c0, across_tol=12.0)
    if wall is None:
        return None
    c, thick = wall
    length = length_pt if length_pt and 0.4 <= length_pt / drawn <= 2.5 else drawn
    mid = (a0 + a1) / 2
    note = ""
    run = _run(geo, horizontal, c, mid)
    if run:
        all_solid = _solid(geo, horizontal, c, thick, *run)
        if sum(e - s for s, e in all_solid) < 0.4 * (run[1] - run[0]):
            # The wall's line work can't be read reliably (e.g. thick masonry with
            # several face lines): keep the panel where Claude placed it along the wall.
            all_solid = [(-1e9, 1e9)]
        # A panel may fill a short wall return that is a touch shorter than it.
        solid = [iv for iv in all_solid if iv[1] - iv[0] >= 0.9 * length - 0.5]
        here = sum(max(0.0, min(e, mid + length / 2) - max(s, mid - length / 2)) for s, e in all_solid)
        if here >= 0.75 * length:  # already (essentially) on solid wall: don't move it
            solid = [(mid - length / 2, mid + length / 2)]
        best = None
        for s, e in solid:
            if e - s < length:  # shorter than the panel: centre it on the stretch
                centre = (s + e) / 2
            else:
                centre = min(max(mid, s + length / 2), e - length / 2)
            if best is None or abs(centre - mid) < abs(best - mid):
                best = centre
        if best is None:
            note = "no stretch of solid wall long enough here"
        elif abs(best - mid) > max(30.0, 2.0 * length):
            note = "nearest solid wall is too far along to move it"
        else:
            if abs(best - mid) > 1.0:
                note = "moved along the wall, off an opening"
            mid = best
    side = SIDE_OFFSET if c0 >= c else -SIDE_OFFSET
    cc = c + side
    s, e = mid - length / 2, mid + length / 2
    if horizontal:
        return pymupdf.Point(s, cc), pymupdf.Point(e, cc), note
    return pymupdf.Point(cc, s), pymupdf.Point(cc, e), note


# ---------------------------------------------------------------------------
# Joists and rafters: ends onto their supports
# ---------------------------------------------------------------------------


def _wall_runs(geo: Geometry, horizontal: bool, min_len: float = 20.0) -> list[tuple[float, float, float, float]]:
    """Continuous walls (c, lo, hi, thickness): wall pieces on the same centreline
    joined across gaps <= 3 pt (jambs, junctions), keeping those >= min_len long."""
    key = ("runs", horizontal, min_len)
    cache = geo.__dict__.setdefault("_cache", {})
    if key in cache:
        return cache[key]
    pieces = sorted(geo.walls[0] if horizontal else geo.walls[1], key=lambda w: (round(w[0]), w[1]))
    runs: list[list[float]] = []  # c, lo, hi, t, n
    for c, lo, hi, t in pieces:
        for r in runs:
            if abs(r[0] - c) <= 1.0 and lo <= r[2] + 3 and hi >= r[1] - 3:
                r[1], r[2] = min(r[1], lo), max(r[2], hi)
                r[3] = (r[3] * r[4] + t) / (r[4] + 1)
                r[4] += 1
                break
        else:
            runs.append([c, lo, hi, t, 1])
    long = [(r[0], r[1], r[2], r[3]) for r in runs if r[2] - r[1] >= min_len]

    def overlap(a, b) -> float:
        return max(0.0, min(a[2], b[2]) - max(a[1], b[1])) / max(1e-6, min(a[2] - a[1], b[2] - b[1]))

    # Decking, stair treads and hatching are many evenly spaced parallel lines whose
    # pairs look like walls; a real wall has at most a couple of neighbours that close.
    out = [w for w in long
           if sum(1 for v in long if v is not w and abs(v[0] - w[0]) <= 8 and overlap(v, w) > 0.5) < 3]
    cache[key] = out
    return out

SPAN_TAGS = {"J", "R", "T"}  # joists, rafters, trusses (span arrows and representative members)

Support = tuple[bool, float, float, float]  # horizontal?, centre c, lo, hi along the support


def snap_span_ends(
    geo: Geometry, p: pymupdf.Point, q: pymupdf.Point, supports: list[Support] = []
) -> tuple[pymupdf.Point, pymupdf.Point, int] | None:
    """Move each end of a joist / rafter line onto the support it bears on: the
    nearest wall centreline, or bearer / beam / ledger on this sheet, that crosses
    the line within reach of that end. The line keeps its position across the
    room (a representative joist can sit anywhere along its supports). An end with
    no support nearby stays put. Returns (p, q, ends moved) or None."""
    horizontal = abs(q.x - p.x) >= abs(q.y - p.y)
    a0, a1 = (p.x, q.x) if horizontal else (p.y, q.y)
    pos = (p.y + q.y) / 2 if horizontal else (p.x + q.x) / 2
    span = abs(a1 - a0)
    if span < 8:
        return None
    end_tol = min(12.0, max(6.0, 0.15 * span))
    # Supports cross the line: real walls (continuous runs >= 20 pt, so joinery,
    # shelving and benchtops don't count) and members on this sheet, perpendicular to it.
    cands = [(c, lo - t - 1, hi + t + 1) for c, lo, hi, t in _wall_runs(geo, not horizontal)]
    cands += [(c, lo - 1, hi + 1) for h, c, lo, hi in supports if h != horizontal]
    cands = [(c, lo, hi) for c, lo, hi in cands if lo <= pos <= hi]

    def snap(v: float, other: float) -> float:
        near = [c for c, _, _ in cands if abs(c - v) <= end_tol and abs(c - other) > 0.5 * span]
        return min(near, key=lambda c: abs(c - v)) if near else v

    b0, b1 = snap(a0, a1), snap(a1, a0)
    moved = (abs(b0 - a0) > 0.05) + (abs(b1 - a1) > 0.05)
    if abs(b1 - b0) < 0.5 * span:  # collapsed: keep Claude's
        return None
    if horizontal:
        return pymupdf.Point(b0, p.y), pymupdf.Point(b1, q.y), moved
    return pymupdf.Point(p.x, b0), pymupdf.Point(q.x, b1), moved
