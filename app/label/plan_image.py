"""Render a plan page with wall IDs drawn on it, so Claude answers by ID, never by position."""

from __future__ import annotations

import pymupdf

from ..model import PlanFrame, Pt, Storey

RENDER_DPI = 110  # enough to read room names and tags; keeps the image ~1 MP


def mm_to_page_pt(frame: PlanFrame, p: Pt, page_height_pt: float) -> pymupdf.Point:
    """Building-local mm -> PyMuPDF page point (top-left origin, y down)."""
    import math

    s = frame.pts_per_mm
    a = math.radians(frame.rotation_deg)
    x = p.x * s
    y = p.y * s
    xr = x * math.cos(a) - y * math.sin(a)
    yr = x * math.sin(a) + y * math.cos(a)
    x_up = frame.origin.x + xr
    y_up = frame.origin.y + yr
    return pymupdf.Point(x_up, page_height_pt - y_up)


def labelled_plan_png(pdf_bytes: bytes, storey: Storey, margin_pt: float = 30) -> bytes:
    """Crop the storey's plan from its page and draw each wall ID at the wall midpoint.

    External walls get a solid green ID box, internal ones orange, so the model can see
    at a glance which walls it is being asked about.
    """
    if storey.frame is None:
        raise ValueError("storey has no PlanFrame")
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = src[storey.frame.page_number - 1]
    h = page.rect.height

    pts = [mm_to_page_pt(storey.frame, p, h) for w in storey.walls for p in (w.start, w.end)]
    if not pts:
        raise ValueError("storey has no walls")
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    clip = pymupdf.Rect(min(xs) - margin_pt, min(ys) - margin_pt, max(xs) + margin_pt, max(ys) + margin_pt) & page.rect

    # Copy the clipped page into a fresh document so we can draw on it without touching the source.
    out = pymupdf.open()
    dst = out.new_page(width=clip.width, height=clip.height)
    dst.show_pdf_page(dst.rect, src, page.number, clip=clip)

    shape = dst.new_shape()
    for w in storey.walls:
        a = mm_to_page_pt(storey.frame, w.start, h) - clip.tl
        b = mm_to_page_pt(storey.frame, w.end, h) - clip.tl
        mid = (a + b) / 2
        colour = (0.0, 0.55, 0.0) if w.external else (0.9, 0.45, 0.0)
        shape.draw_line(a, b)
        shape.finish(color=colour, width=1.2, dashes="[2 2]" if not w.external else None)
        label = w.id
        tw = pymupdf.get_text_length(label, fontname="helv", fontsize=6) + 3
        box = pymupdf.Rect(mid.x - tw / 2, mid.y - 4.5, mid.x + tw / 2, mid.y + 4.5)
        shape.draw_rect(box)
        shape.finish(color=colour, fill=(1, 1, 1), width=0.6)
        shape.insert_text(pymupdf.Point(box.x0 + 1.5, box.y1 - 2), label, fontname="helv", fontsize=6, color=colour)
    shape.commit()

    zoom = RENDER_DPI / 72
    pix = dst.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def page_png(pdf_bytes: bytes, page_number: int, dpi: int = 80) -> bytes:
    """Whole page at low resolution - for roof plans / elevations as context."""
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = src[page_number - 1]
    zoom = dpi / 72
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
