"""Step 2 - vector geometry of one page.

Reads `page.get_drawings()` and turns it into three plain lists, all in PDF user
space (points, origin bottom-left, y up):

* `segments`  - straight strokes, axis-snapped and merged along their line, with pen
                width kept (walls are drawn with the heaviest pen; windows, fixtures
                and dimension lines with lighter ones);
* `bars`      - thin dark filled rectangles (how some offices draw walls: a solid
                black bar per wall segment);
* `curves`    - bounding boxes of bezier curves (door swings), kept as evidence only.

Failure modes: a page that is a scanned image has no drawings at all (`raw_count`
is 0 - the caller reports it); walls drawn with the same pen as everything else and
without fills cannot be told apart from furniture by this step alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf

from .geom import Bar, Seg, merge_collinear, snap_to_axis

MIN_SEGMENT_PT = 0.8  # anything shorter is a rendering artefact
DARK_FILL_MAX_LUMA = 0.5  # fills darker than this count as "drawn solid"


@dataclass
class FillLoop:
    """One closed loop of a filled path (a path can hold several), as y-up points."""

    points: list[tuple[float, float]]
    luma: float


@dataclass
class VectorSet:
    page_number: int
    segments: list[Seg] = field(default_factory=list)
    bars: list[Bar] = field(default_factory=list)
    curves: list[tuple[float, float, float, float]] = field(default_factory=list)
    raw_count: int = 0
    width_classes: dict[float, float] = field(default_factory=dict)  # pen width -> total length of long axis segments


def page_to_pdf(page: pymupdf.Page) -> pymupdf.Matrix:
    """Matrix taking PyMuPDF page coordinates (top-left, y down) to PDF user space."""
    return page.transformation_matrix


def pdf_to_page(page: pymupdf.Page) -> pymupdf.Matrix:
    return ~page.transformation_matrix


def _luma(rgb) -> float:
    if rgb is None:
        return 1.0
    r, g, b = (list(rgb) + [0, 0, 0])[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _loops(items, to_pdf: pymupdf.Matrix) -> list[list[tuple[float, float]]]:
    """Split a path's items into closed loops. A new loop starts wherever an item does
    not continue from the previous item's end point."""
    loops: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    last = None
    for it in items:
        op = it[0]
        if op == "re":
            r = it[1]
            pts = [r.bl, r.br, r.tr, r.tl]  # page coords; orientation irrelevant
            if cur:
                loops.append(cur)
            cur = [tuple(p * to_pdf) for p in pts]
            loops.append(cur)
            cur, last = [], None
            continue
        if op == "l":
            a, b = it[1], it[2]
        elif op == "c":
            a, b = it[1], it[4]
        elif op == "qu":
            q = it[1]
            a, b = q.ul, q.ul
            pts = [q.ul, q.ur, q.lr, q.ll]
            if cur:
                loops.append(cur)
            loops.append([tuple(p * to_pdf) for p in pts])
            cur, last = [], None
            continue
        else:
            continue
        if last is None or abs(a.x - last.x) > 1e-3 or abs(a.y - last.y) > 1e-3:
            if cur:
                loops.append(cur)
            cur = [tuple(a * to_pdf)]
        cur.append(tuple(b * to_pdf))
        last = b
    if cur:
        loops.append(cur)
    return loops


def _rect_bar(points: list[tuple[float, float]], tol: float = 0.25) -> Bar | None:
    """If the loop is an axis-aligned rectangle, return it as a Bar; else None."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0, y1 - y0
    if w < tol or h < tol:
        return None
    # every vertex must sit on the bbox boundary
    for x, y in points:
        on_x = abs(x - x0) <= tol or abs(x - x1) <= tol
        on_y = abs(y - y0) <= tol or abs(y - y1) <= tol
        if not (on_x or on_y):
            return None
    if w >= h:
        return Bar("h", (y0 + y1) / 2, x0, x1, h, source="fill")
    return Bar("v", (x0 + x1) / 2, y0, y1, w, source="fill")


def extract_vectors(page: pymupdf.Page) -> VectorSet:
    to_pdf = page_to_pdf(page)
    drawings = page.get_drawings()
    vs = VectorSet(page_number=page.number + 1, raw_count=len(drawings))
    raw_segs: list[Seg] = []

    for d in drawings:
        kind = d["type"]  # 's' stroke, 'f' fill, 'fs' both
        width = float(d.get("width") or 0.0)
        dashed = bool(d.get("dashes") and d["dashes"] not in ("[] 0", "", None))
        stroked = "s" in kind
        filled = "f" in kind and d.get("fill") is not None
        luma = _luma(d.get("fill")) if filled else 1.0

        if stroked:
            for it in d["items"]:
                if it[0] == "l":
                    a, b = it[1] * to_pdf, it[2] * to_pdf
                    raw_segs.append(Seg(a.x, a.y, b.x, b.y, width, "stroke", dashed))
                elif it[0] == "c":
                    pts = [p * to_pdf for p in it[1:5]]
                    xs = [p.x for p in pts]
                    ys = [p.y for p in pts]
                    vs.curves.append((min(xs), min(ys), max(xs), max(ys)))
                elif it[0] == "re":
                    r = it[1]
                    corners = [p * to_pdf for p in (r.tl, r.tr, r.br, r.bl)]
                    for i in range(4):
                        a, b = corners[i], corners[(i + 1) % 4]
                        raw_segs.append(Seg(a.x, a.y, b.x, b.y, width, "stroke", dashed))

        if filled and luma <= DARK_FILL_MAX_LUMA:
            for loop in _loops(d["items"], to_pdf):
                if len(loop) < 3:
                    continue
                bar = _rect_bar(loop)
                if bar is not None:
                    vs.bars.append(bar)
                else:
                    # non-rectangular solid: keep its edges so the line-pair logic can use them
                    for i in range(len(loop)):
                        a, b = loop[i], loop[(i + 1) % len(loop)]
                        raw_segs.append(Seg(a[0], a[1], b[0], b[1], 0.0, "fill", False))

    snapped = [snap_to_axis(s) for s in raw_segs if s.length >= 1e-3]
    merged = merge_collinear(snapped)
    vs.segments = [s for s in merged if s.length >= MIN_SEGMENT_PT]

    for s in vs.segments:
        if s.axis and s.length >= 10 and s.kind == "stroke":
            key = round(s.width, 2)
            vs.width_classes[key] = vs.width_classes.get(key, 0.0) + s.length
    return vs
