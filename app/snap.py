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
