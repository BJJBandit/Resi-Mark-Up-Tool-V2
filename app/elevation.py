"""Elevation views: find window / door openings, keep articulation joints off them,
and cross-check the openings against the plan.

Elevations are drawn with lines (frames, mullions, cladding, roof ribs, brick
coursing), not with rectangles, so openings are found as boxes closed by line
work: two vertical lines with the same top and bottom, joined by horizontal lines
along both. Boxes full of repeated verticals (roof ribs, cladding, brick) are not
openings; boxes inside another box (sashes, panes) are merged into the outer frame.
"""

from __future__ import annotations

from collections import defaultdict

import pymupdf

from .snap import Geometry, mm_to_pt


def _covered(segs, a: float, b: float, tol: float = 1.0) -> bool:
    """Do the segments (a, b along one line) cover a..b?"""
    pts = sorted((s.a, s.b) for s in segs if s.b >= a - tol and s.a <= b + tol)
    reach = a
    for s, e in pts:
        if s > reach + tol:
            return False
        reach = max(reach, e)
    return reach >= b - tol


def find_openings(geo: Geometry, scale: int | None, min_mm: int = 400, max_mm: int = 5500) -> list[pymupdf.Rect]:
    """Window / door frames on an elevation page, as rectangles (page coords)."""
    if not scale:
        return []
    lo_pt, hi_pt = mm_to_pt(min_mm, scale), mm_to_pt(max_mm, scale)
    min_h = mm_to_pt(400, scale)
    verts = [s for s in geo.v if s.b - s.a >= min_h]
    by_span: dict[tuple[int, int], list] = defaultdict(list)
    for s in verts:
        by_span[(round(s.a), round(s.b))].append(s)
    h_by_y: dict[int, list] = defaultdict(list)
    for s in geo.h:
        h_by_y[round(s.c)].append(s)

    def h_at(y: float):
        return [s for k in (round(y) - 1, round(y), round(y) + 1) for s in h_by_y.get(k, []) if abs(s.c - y) <= 0.8]

    boxes: list[pymupdf.Rect] = []
    for (ya, yb), group in by_span.items():
        xs = sorted({round(s.c, 1) for s in group})
        # Roof sheeting ribs / cladding battens: many identical verticals at a small,
        # regular pitch. Window frames and mullions never form such a run.
        if len(xs) >= 8:
            gaps = sorted(b - a for a, b in zip(xs, xs[1:]))
            if gaps[len(gaps) // 2] < 10:
                continue
        for i, x0 in enumerate(xs):
            for x1 in xs[i + 1:]:
                w = x1 - x0
                if w < lo_pt:
                    continue
                if w > hi_pt:
                    break
                top, bot = min(ya, yb), max(ya, yb)
                if not (_covered(h_at(top), x0, x1) and _covered(h_at(bot), x0, x1)):
                    continue
                # Roof ribs / cladding / brick: many full-height verticals inside.
                inside = [s for s in verts if x0 + 0.5 < s.c < x1 - 0.5 and s.a <= top + 1 and s.b >= bot - 1]
                if len({round(s.c) for s in inside}) > 3:
                    continue
                boxes.append(pymupdf.Rect(x0, top, x1, bot))
    # Keep outer frames only.
    boxes.sort(key=lambda r: -r.get_area())
    out: list[pymupdf.Rect] = []
    for r in boxes:
        if not any(o.contains(r) or (o & r).get_area() > 0.6 * r.get_area() for o in out):
            out.append(r)
    # Side-by-side frames sharing head and sill (a window with a mullion drawn as two
    # boxes) are one opening.
    out.sort(key=lambda r: (round(r.y0), r.x0))
    merged: list[pymupdf.Rect] = []
    for r in out:
        last = merged[-1] if merged else None
        if last and abs(last.y0 - r.y0) <= 1.5 and abs(last.y1 - r.y1) <= 1.5 and 0 <= r.x0 - last.x1 <= 2.5 \
                and r.x1 - last.x0 <= hi_pt:
            merged[-1] = pymupdf.Rect(last.x0, min(last.y0, r.y0), r.x1, max(last.y1, r.y1))
        else:
            merged.append(pymupdf.Rect(r))
    return merged


def snap_joint_elevation(p: pymupdf.Point, q: pymupdf.Point, openings: list[pymupdf.Rect]):
    """An articulation joint on an elevation is a vertical line through the masonry.
    Make it vertical; if it runs through a window / door, move it to the nearest
    side (jamb) of that opening. Returns (p, q, note)."""
    x = (p.x + q.x) / 2
    top, bot = min(p.y, q.y), max(p.y, q.y)
    note = ""
    for r in openings:
        if r.x0 < x < r.x1 and min(bot, r.y1) - max(top, r.y0) > 0:
            x = r.x0 if x - r.x0 <= r.x1 - x else r.x1
            note = "moved out of an opening to its jamb"
            break
    return pymupdf.Point(x, top), pymupdf.Point(x, bot), note


def cross_check(plan_widths_mm: list[int], elev_openings: list[pymupdf.Rect], scale: int, tol_mm: int = 150) -> list[str]:
    """Compare the plan's opening widths (from its codes) with the openings found on
    the elevations. Returns plain-English mismatches (counts per width)."""
    elev = sorted(round(r.width * 25.4 / 72 * scale) for r in elev_openings)
    plan = sorted(plan_widths_mm)
    unmatched_plan, unmatched_elev = [], list(elev)
    for w in plan:
        m = next((e for e in unmatched_elev if abs(e - w) <= tol_mm), None)
        if m is None:
            unmatched_plan.append(w)
        else:
            unmatched_elev.remove(m)
    out = []
    if unmatched_plan:
        out.append("plan openings with no matching opening on the elevations: "
                   + ", ".join(f"{w} wide" for w in unmatched_plan))
    if unmatched_elev:
        out.append("elevation openings with no matching coded opening on the plan: "
                   + ", ".join(f"~{round(w, -1)} wide" for w in unmatched_elev))
    return out
