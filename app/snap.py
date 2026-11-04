"""Snap lintels onto the real openings in a vector (CAD-exported) drawing.

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
