"""Render a page with the extraction result drawn over it.

    python -m app.extract.debug <pdf> <page> [out_dir]

Green = wall centre-lines (width proportional to thickness; dashed = internal),
red = openings, blue = dimension chains that agreed with the page scale,
magenta = plan frame origin and plan outline, grey text = plan label.
"""

from __future__ import annotations

import os
import sys

import pymupdf

from .dimensions import analyse_dimensions
from .geom import pts_per_mm
from .pdf_pages import scan_page, title_block_scale
from .vector import extract_vectors, pdf_to_page
from .walls import detect_walls

DEFAULT_OUT = (
    "/tmp/claude-0/-home-user-Resi-Mark-Up-Tool-V2/42dbd95c-140c-5d18-86a9-a6085979f2a3/scratchpad/extract/"
)


def choose_scale(scan, dims) -> float | None:
    """Same rule as extract.py: measured scale when confident, else the text."""
    if dims.scale_measured and dims.n_agree >= 3:
        return round(dims.scale_measured)
    tb = title_block_scale(scan)
    if tb:
        return tb.denominator
    if scan.scales:
        return scan.scales[0].denominator
    return None


def render_overlay(pdf_path: str, page_number: int, out_dir: str = DEFAULT_OUT, zoom: float = 2.0) -> str:
    doc = pymupdf.open(pdf_path)
    page = doc[page_number - 1]
    scan = scan_page(page)
    vs = extract_vectors(page)
    dims = analyse_dimensions(scan, vs)
    scale = choose_scale(scan, dims)
    walls = detect_walls(scan, vs, scale, [d.word.bbox for d in dims.dims if d.consistent])
    m = pdf_to_page(page)
    ppm = pts_per_mm(scale or 100.0)

    shape = page.new_shape()

    def P(x, y):
        return pymupdf.Point(x, y) * m

    for c in dims.chains:
        for d in c.dims:
            a, b = (P(d.lo, d.pos), P(d.hi, d.pos)) if d.axis == "h" else (P(d.pos, d.lo), P(d.pos, d.hi))
            shape.draw_line(a, b)
            shape.draw_circle(a, 1.2)
            shape.draw_circle(b, 1.2)
    shape.finish(color=(0.1, 0.3, 1.0), width=0.8)

    for plan in walls.plans:
        for r in plan.runs:
            a, b = P(*r.point(r.lo)), P(*r.point(r.hi))
            shape.draw_line(a, b)
        shape.finish(color=(0.0, 0.6, 0.0), width=1.6)
        for r in plan.runs:
            if r.external:
                a, b = P(*r.point(r.lo)), P(*r.point(r.hi))
                shape.draw_line(a, b)
        shape.finish(color=(0.0, 0.45, 0.0), width=3.2)
        for r in plan.runs:
            for o in r.openings:
                a, b = P(*r.point(o.lo)), P(*r.point(o.hi))
                shape.draw_line(a, b)
        shape.finish(color=(1.0, 0.0, 0.0), width=3.0)
        x0, y0, x1, y1 = plan.bbox
        shape.draw_rect(pymupdf.Rect(P(x0, y1), P(x1, y0)))
        shape.draw_circle(P(x0, y0), 4)
        shape.finish(color=(0.9, 0.0, 0.9), width=0.8)
        shape.insert_text(P(x0, y1) + (0, -4), f"{plan.label} [{'closed' if plan.closed else 'OPEN'}]", fontsize=8, color=(0.5, 0.5, 0.5))
    shape.commit()

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    job = os.path.basename(os.path.dirname(os.path.abspath(pdf_path)))
    out = os.path.join(out_dir, f"{job}_{base}_p{page_number}.png")
    page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(out)

    print(f"page {page_number}: kind={scan.kind} title={scan.title!r} sheet={scan.sheet_size} vector={scan.is_vector}")
    print(f"  scale: text={[s.text for s in scan.scales]} measured={dims.scale_measured and round(dims.scale_measured, 2)} "
          f"votes={dims.n_agree}/{dims.n_votes} chosen={scale}")
    print(f"  walls: mode={walls.mode} heavy_pen={walls.heavy_width} bars={len(walls.bars)} plans={len(walls.plans)} dropped={walls.dropped_clusters}")
    for plan in walls.plans:
        x0, y0, x1, y1 = plan.bbox
        ext = [r for r in plan.runs if r.external]
        n_open = sum(len(r.openings) for r in plan.runs)
        print(f"  plan {plan.label!r}: {len(plan.runs)} walls ({len(ext)} external, {'closed' if plan.closed else 'OPEN'}), "
              f"{n_open} openings, extents {((x1 - x0) / ppm):.0f} x {((y1 - y0) / ppm):.0f} mm")
        thick = sorted({round(r.thickness / ppm) for r in plan.runs})
        print(f"    thicknesses mm: {thick}")
        for r in plan.runs:
            for o in r.openings:
                print(f"    opening {o.kind:12s} {o.width_pt / ppm:6.0f} mm  tag={o.tag_text!r} via {o.evidence}")
    if walls.unmatched_tags:
        print(f"  unmatched tags: {walls.unmatched_tags}")
    print(f"  overlay: {out}")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    render_overlay(sys.argv[1], int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else DEFAULT_OUT)
