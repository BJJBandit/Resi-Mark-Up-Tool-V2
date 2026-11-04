"""The building footprint on a plan: the area enclosed by the building's walls.

Lintels and bracing must be contained within the building, so anything placed on a
deck, verandah, carport or eaves line is moved onto a building wall or removed.

The building's walls are the largest connected network of wall centrelines in the
drawing (external and internal walls join up; decks, fences and eaves don't form
walls). They are rasterised on a coarse grid, the outside is flood-filled from the
edges (wide openings on a wall line bridged first), and everything not reached - the
walls and the rooms they enclose - is the
building. Plain Python (no numpy) so it runs anywhere.
"""

from __future__ import annotations

from collections import deque

import pymupdf

from .snap import Geometry, Seg, _wall_runs

CELL = 2.0  # pt per grid cell


def _drop_regular(segs: list[Seg]) -> list[Seg]:
    by_c: dict[int, list[Seg]] = {}
    for g in segs:
        by_c.setdefault(round(g.c), []).append(g)

    def partner(g: Seg, c: float) -> Seg | None:
        for k in (round(c) - 1, round(c), round(c) + 1):
            for o in by_c.get(k, ()):
                # Array lines share (or, raked, gradually change) their extent.
                if abs(o.c - c) <= 0.3 and abs(o.a - g.a) <= 15 and abs(o.b - g.b) <= 15:
                    return o
        return None

    array: set[int] = set()
    for g in segs:
        if id(g) in array or g.b - g.a < 8:
            continue
        # Candidate spacings: the next lines above that overlap this one.
        nxt = sorted({round(o.c - g.c, 1) for k in range(round(g.c) + 3, round(g.c) + 9)
                      for o in by_c.get(k, ()) if 3 <= o.c - g.c <= 8
                      and min(o.b, g.b) - max(o.a, g.a) >= 0.3 * min(o.b - o.a, g.b - g.a)})
        for sp in nxt:
            chain, cur = [g], g
            while (o := partner(cur, cur.c + sp)) is not None:
                chain.append(o)
                cur = o
            cur = g
            while (o := partner(cur, cur.c - sp)) is not None:
                chain.append(o)
                cur = o
            if len(chain) >= 5:
                array.update(id(o) for o in chain)
                break
    return [g for g in segs if id(g) not in array]


class Footprint:
    def __init__(self, geo: Geometry, region: pymupdf.Rect | None = None) -> None:
        geo = self._without_arrays(geo)
        walls = [(True, c, lo, hi, t) for c, lo, hi, t in _wall_runs(geo, True)] + \
                [(False, c, lo, hi, t) for c, lo, hi, t in _wall_runs(geo, False)]
        if region is not None:
            walls = [w for w in walls if self._in(w, region)]
        self.walls = self._largest_network(walls)
        self.ok = len(self.walls) >= 4
        if not self.ok:
            return
        xs = [v for h, c, lo, hi, t in self.walls for v in ((lo, hi) if h else (c,))]
        ys = [v for h, c, lo, hi, t in self.walls for v in ((c,) if h else (lo, hi))]
        pad = 3 * CELL
        self.x0, self.y0 = min(xs) - pad, min(ys) - pad
        self.nx = int((max(xs) + pad - self.x0) / CELL) + 1
        self.ny = int((max(ys) + pad - self.y0) / CELL) + 1
        wall = bytearray(self.nx * self.ny)
        for h, c, lo, hi, t in self.walls + self._bridges(self.walls):
            half = max(t / 2, CELL)
            if h:
                self._fill(wall, lo, c - half, hi, c + half)
            else:
                self._fill(wall, c - half, lo, c + half, hi)
        # Close small gaps (door openings drawn as gaps in both faces) so rooms
        # aren't flood-filled from outside through a doorway.
        wall = self._dilate(wall, 6)
        outside = bytearray(self.nx * self.ny)
        q = deque()
        for i in range(self.nx):
            for j in (0, self.ny - 1):
                q.append((i, j))
        for j in range(self.ny):
            for i in (0, self.nx - 1):
                q.append((i, j))
        while q:
            i, j = q.popleft()
            k = j * self.nx + i
            if outside[k] or wall[k]:
                continue
            outside[k] = 1
            if i > 0:
                q.append((i - 1, j))
            if i < self.nx - 1:
                q.append((i + 1, j))
            if j > 0:
                q.append((i, j - 1))
            if j < self.ny - 1:
                q.append((i, j + 1))
        self.inside = bytearray(1 - v for v in outside)

    @staticmethod
    def _in(w, region: pymupdf.Rect) -> bool:
        h, c, lo, hi, t = w
        r = pymupdf.Rect(lo, c, hi, c) if h else pymupdf.Rect(c, lo, c, hi)
        return region.intersects(r + (-1, -1, 1, 1))

    @staticmethod
    def _without_arrays(geo: Geometry) -> Geometry:
        """A copy of the line work without regular arrays of parallel lines - roof
        sheeting, eaves hatching, decking boards, stair treads - whose neighbouring
        pairs look like walls and would join decks and eaves to the building. An
        array is 5+ lines in a row at an equal spacing of 3-8 pt; a wall's face
        lines (brick, cavity, stud, lining) are never that regular."""
        out = Geometry.__new__(Geometry)
        out.h, out.v = _drop_regular(geo.h), _drop_regular(geo.v)
        return out

    @staticmethod
    def _bridges(walls, max_gap: float = 140.0):
        """Close wide openings (sliding doors, stacker doors, garage doors) by joining
        building wall runs on the same line across the gap between them."""
        out = []
        for h0 in (True, False):
            runs = sorted((w for w in walls if w[0] == h0), key=lambda w: (w[1], w[2]))
            for i, (h, c, lo, hi, t) in enumerate(runs):
                best = None
                for _, c2, lo2, hi2, t2 in runs:
                    if abs(c2 - c) <= 3 and lo2 > hi and lo2 - hi <= max_gap and (best is None or lo2 < best[0]):
                        best = (lo2, c2, t2)
                if best and not any(abs(c3 - c) <= 3 and lo3 < best[0] and hi3 > hi for _, c3, lo3, hi3, _ in runs):
                    out.append((h, (c + best[1]) / 2, hi, best[0], min(t, best[2])))
        return out

    @staticmethod
    def _largest_network(walls):
        """Group walls that touch or cross (within 4 pt); keep the group with the
        most wall length - the building."""
        n = len(walls)
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def box(w):
            h, c, lo, hi, t = w
            return pymupdf.Rect(lo, c - t / 2, hi, c + t / 2) if h else pymupdf.Rect(c - t / 2, lo, c + t / 2, hi)

        boxes = [box(w) + (-4, -4, 4, 4) for w in walls]
        for i in range(n):
            for j in range(i + 1, n):
                if boxes[i].intersects(boxes[j]):
                    parent[find(i)] = find(j)
        groups: dict[int, list] = {}
        for i, w in enumerate(walls):
            groups.setdefault(find(i), []).append(w)
        if not groups:
            return []
        return max(groups.values(), key=lambda g: sum(w[3] - w[2] for w in g))

    def _fill(self, grid, x0, y0, x1, y1) -> None:
        i0, i1 = max(0, int((x0 - self.x0) / CELL)), min(self.nx - 1, int((x1 - self.x0) / CELL))
        j0, j1 = max(0, int((y0 - self.y0) / CELL)), min(self.ny - 1, int((y1 - self.y0) / CELL))
        for j in range(j0, j1 + 1):
            row = j * self.nx
            for i in range(i0, i1 + 1):
                grid[row + i] = 1

    def _dilate(self, grid, r: int):
        """Grow walls by r cells along rows then columns (closes gaps up to ~2r cells)."""
        nx, ny = self.nx, self.ny
        tmp = bytearray(grid)
        for j in range(ny):
            row = j * nx
            for i in range(nx):
                if grid[row + i]:
                    for d in range(1, r + 1):
                        if i + d < nx:
                            tmp[row + i + d] = 1
                        if i - d >= 0:
                            tmp[row + i - d] = 1
        out = bytearray(tmp)
        for j in range(ny):
            for i in range(nx):
                if tmp[j * nx + i]:
                    for d in range(1, r + 1):
                        if j + d < ny:
                            out[(j + d) * nx + i] = 1
                        if j - d >= 0:
                            out[(j - d) * nx + i] = 1
        # Shrink back by r so the footprint keeps its true outline (closing).
        return self._erode(out, r, keep=grid)

    def _erode(self, grid, r: int, keep):
        nx, ny = self.nx, self.ny
        tmp = bytearray(grid)
        for j in range(ny):
            row = j * nx
            for i in range(nx):
                if grid[row + i] and not keep[row + i]:
                    for d in range(1, r + 1):
                        if (i + d < nx and not grid[row + i + d]) or (i - d >= 0 and not grid[row + i - d]):
                            tmp[row + i] = 0
                            break
        out = bytearray(tmp)
        for j in range(ny):
            for i in range(nx):
                k = j * nx + i
                if tmp[k] and not keep[k]:
                    for d in range(1, r + 1):
                        if (j + d < ny and not tmp[(j + d) * nx + i]) or (j - d >= 0 and not tmp[(j - d) * nx + i]):
                            out[k] = 0
                            break
        return out

    def contains(self, pt: pymupdf.Point, tol: float = 2.0) -> bool:
        """Is the point inside the building (rooms or walls), allowing tol pt?"""
        if not self.ok:
            return True  # can't tell: don't move anything
        r = max(0, int(tol / CELL))
        i0, j0 = int((pt.x - self.x0) / CELL), int((pt.y - self.y0) / CELL)
        for j in range(j0 - r, j0 + r + 1):
            for i in range(i0 - r, i0 + r + 1):
                if 0 <= i < self.nx and 0 <= j < self.ny and self.inside[j * self.nx + i]:
                    return True
        return False

    def line_inside(self, p: pymupdf.Point, q: pymupdf.Point, tol: float = 2.0, frac: float = 0.9) -> bool:
        """At least frac of the line's sample points are inside the building."""
        n = max(2, int(abs(q - p) / CELL))
        pts = [p + (q - p) * (k / n) for k in range(n + 1)]
        return sum(self.contains(x, tol) for x in pts) >= frac * len(pts)

    def move_inside(self, p: pymupdf.Point, q: pymupdf.Point, reach: float = 25.0):
        """Slide a line sideways onto the nearest parallel building wall line (within
        reach) where it then lies inside the building. Returns (p, q) or None."""
        h0 = abs(q.x - p.x) >= abs(q.y - p.y)
        c0 = (p.y + q.y) / 2 if h0 else (p.x + q.x) / 2
        lo0, hi0 = sorted((p.x, q.x) if h0 else (p.y, q.y))
        cands = sorted({round(c, 2) for h, c, lo, hi, t in self.walls
                        if h == h0 and abs(c - c0) <= reach and lo <= hi0 + 20 and hi >= lo0 - 20},
                       key=lambda c: abs(c - c0))
        for c in cands:
            # Keep it along the wall: slide it back within the wall's ends if it overhangs.
            ends = [(lo, hi) for h, c2, lo, hi, t in self.walls if h == h0 and abs(c2 - c) <= 1.5]
            w0, w1 = min(e[0] for e in ends), max(e[1] for e in ends)
            lo, hi = lo0, hi0
            if hi - lo <= w1 - w0:
                shift = max(0.0, w0 - lo) - max(0.0, hi - w1)
                lo, hi = lo + shift, hi + shift
            a, b = (pymupdf.Point(lo, c), pymupdf.Point(hi, c)) if h0 else (pymupdf.Point(c, lo), pymupdf.Point(c, hi))
            if self.line_inside(a, b, tol=4):
                return a, b
        return None

    def nearest_wall(self, p: pymupdf.Point, q: pymupdf.Point, reach: float = 25.0):
        """The building wall (h, c, lo, hi, t) parallel to the line nearest to it, within reach."""
        h0 = abs(q.x - p.x) >= abs(q.y - p.y)
        c0 = (p.y + q.y) / 2 if h0 else (p.x + q.x) / 2
        lo0, hi0 = sorted((p.x, q.x) if h0 else (p.y, q.y))
        best = None
        for w in self.walls:
            h, c, lo, hi, t = w
            if h != h0 or min(hi, hi0) - max(lo, lo0) < 0.3 * (hi0 - lo0):
                continue
            d = abs(c - c0)
            if d <= reach and (best is None or d < best[0]):
                best = (d, w)
        return best[1] if best else None

    def mask_image(self, page: pymupdf.Page) -> None:
        """Debug: draw the footprint cells onto a page."""
        sh = page.new_shape()
        for j in range(self.ny):
            for i in range(self.nx):
                if self.inside[j * self.nx + i]:
                    x, y = self.x0 + i * CELL, self.y0 + j * CELL
                    sh.draw_rect(pymupdf.Rect(x, y, x + CELL, y + CELL))
        sh.finish(color=None, fill=(0, 0.6, 1), fill_opacity=0.25)
        sh.commit()
