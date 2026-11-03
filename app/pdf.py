"""PDF helpers: render architectural pages for Claude and build the mark-up set."""

from __future__ import annotations

import datetime as dt
import textwrap
from dataclasses import dataclass

import pymupdf

from .schemas import (
    SHEET_TITLES,
    DesignAssessment,
    GeotechReview,
    Mark,
    MarkupSheet,
    SheetKind,
)

MAX_PAGES = 20
MAX_EDGE_PX = 1568  # long edge sent to Claude; keeps detail without oversizing

# Colour per sheet (RGB 0-1). Annotations stay editable in Bluebeam / Acrobat.
SHEET_COLOURS: dict[SheetKind, tuple[float, float, float]] = {
    SheetKind.LOAD_PATHS: (0.85, 0.10, 0.10),
    SheetKind.ROOF: (0.00, 0.45, 0.85),
    SheetKind.WALLS: (0.00, 0.60, 0.25),
    SheetKind.FOOTINGS: (0.60, 0.20, 0.70),
    SheetKind.FLOOR: (0.90, 0.50, 0.00),
}


@dataclass
class PageImage:
    number: int  # 1-based
    width: int
    height: int
    jpeg: bytes
    text: str


def render_pages(pdf_bytes: bytes) -> list[PageImage]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count == 0:
        raise ValueError("Architectural PDF has no pages.")
    if doc.page_count > MAX_PAGES:
        raise ValueError(f"Architectural PDF has {doc.page_count} pages; limit is {MAX_PAGES}.")
    out = []
    for i, page in enumerate(doc):
        rect = page.rect  # rotated (as displayed) page size in points
        zoom = MAX_EDGE_PX / max(rect.width, rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        out.append(
            PageImage(
                number=i + 1,
                width=pix.width,
                height=pix.height,
                jpeg=pix.tobytes("jpeg", jpg_quality=85),
                text=page.get_text("text").strip(),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Output document
# ---------------------------------------------------------------------------

def _clamp(v: float) -> float:
    return min(max(v, 0.0), 1.0)


def _disp(page: pymupdf.Page, x: float, y: float) -> pymupdf.Point:
    """Normalised image coords -> displayed (rotated) page coords."""
    r = page.rect
    return pymupdf.Point(r.x0 + _clamp(x) * r.width, r.y0 + _clamp(y) * r.height)


def _to_page(page: pymupdf.Page, x: float, y: float) -> pymupdf.Point:
    """Normalised image coords -> unrotated page coords used by annotations."""
    return _disp(page, x, y) * page.derotation_matrix


def _derotate_rect(page: pymupdf.Page, r: pymupdf.Rect) -> pymupdf.Rect:
    m = page.derotation_matrix
    return pymupdf.Rect(r.tl * m, r.br * m).normalize()


def _add_mark(page: pymupdf.Page, m: Mark, colour, fontsize: float) -> None:
    a = _to_page(page, m.x1, m.y1)
    b = _to_page(page, m.x2, m.y2)
    rect = pymupdf.Rect(a, b).normalize()
    info = {"title": m.category, "content": m.label}

    if m.shape in ("rect", "cloud"):
        if rect.is_empty:
            rect = pymupdf.Rect(a.x - 6, a.y - 6, a.x + 6, a.y + 6)
        annot = page.add_rect_annot(rect)
        annot.set_colors(stroke=colour)
        annot.set_border(width=1.5, dashes=[4, 2] if m.shape == "cloud" else None)
        annot.set_info(info)
        annot.update(opacity=0.9)
        anchor = _disp(page, min(m.x1, m.x2), min(m.y1, m.y2))
    elif m.shape in ("line", "arrow"):
        annot = page.add_line_annot(a, b)
        annot.set_colors(stroke=colour)
        annot.set_border(width=1.8)
        if m.shape == "arrow":
            annot.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
            annot.set_colors(stroke=colour, fill=colour)
        annot.set_info(info)
        annot.update()
        anchor = _disp(page, m.x1, m.y1)
    else:  # text only
        anchor = _disp(page, m.x1, m.y1)

    if m.label:
        width = pymupdf.get_text_length(m.label, fontname="helv", fontsize=fontsize) + 8
        box = _derotate_rect(page, pymupdf.Rect(anchor.x, anchor.y - fontsize * 1.6, anchor.x + width, anchor.y))
        t = page.add_freetext_annot(
            box,
            m.label,
            fontsize=fontsize,
            text_color=colour,
            fill_color=(1, 1, 1),
            rotate=page.rotation,
        )
        t.set_info(info)
        t.update()


def _notes_panel(page: pymupdf.Page, title: str, colour, legend: list[str], notes: list[str]) -> None:
    """Notes panel in the displayed bottom-right corner of the sheet."""
    r = page.rect
    w = min(r.width * 0.32, 320)
    lines = [title, ""]
    if legend:
        lines += ["LEGEND:"] + [f"  {c}" for c in legend] + [""]
    if notes:
        lines += ["NOTES:"] + [f"{i}. {n}" for i, n in enumerate(notes, 1)] + [""]
    lines.append("PRELIMINARY - FOR ENGINEER REVIEW. NOT FOR CONSTRUCTION.")
    text = "\n".join(lines)
    fontsize = max(6.0, min(9.0, r.width / 160))
    est_lines = sum(1 + len(l) * fontsize * 0.5 // w for l in lines)
    h = min(r.height * 0.6, est_lines * fontsize * 1.3 + 12)
    disp = pymupdf.Rect(r.x1 - w - 10, r.y1 - h - 10, r.x1 - 10, r.y1 - 10)
    box = _derotate_rect(page, disp)
    annot = page.add_freetext_annot(
        box,
        text,
        fontsize=fontsize,
        text_color=(0, 0, 0),
        fill_color=(1, 1, 0.92),
        rotate=page.rotation,
    )
    annot.set_border(width=1)
    annot.set_info({"title": "Mark-up notes", "content": text})
    annot.update()


def _banner(page: pymupdf.Page, title: str, colour) -> None:
    r = page.rect
    fontsize = max(10.0, min(20.0, r.width / 60))
    text = f"MARK-UP: {title}"
    width = pymupdf.get_text_length(text, fontname="helv", fontsize=fontsize) + 12
    disp = pymupdf.Rect(r.x0 + 10, r.y0 + 10, r.x0 + 10 + width, r.y0 + 10 + fontsize * 1.8)
    box = _derotate_rect(page, disp)
    annot = page.add_freetext_annot(
        box,
        text,
        fontsize=fontsize,
        text_color=(1, 1, 1),
        fill_color=colour,
        rotate=page.rotation,
    )
    annot.update()


def _write_lines(doc: pymupdf.Document, heading: str, lines: list[str]) -> None:
    """Plain A4 text pages, wrapped and flowed onto as many pages as needed."""
    wrapped: list[str] = []
    for line in lines:
        indent = len(line) - len(line.lstrip())
        wrapped += textwrap.wrap(line, 105, subsequent_indent=" " * (indent + 2)) or [""]
    per_page = 72
    for n, i in enumerate(range(0, len(wrapped), per_page)):
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 45), heading + (" (cont.)" if n else ""), fontsize=13, fontname="hebo")
        page.insert_text((40, 70), "\n".join(wrapped[i : i + per_page]), fontsize=8.5, fontname="helv", lineheight=1.25)


def _summary_lines(project: str, geotech: GeotechReview, design: DesignAssessment) -> list[str]:
    def bullets(items: list[str]) -> list[str]:
        return [f"  - {i}" for i in items] or ["  - none"]

    g, d = geotech, design
    lines = [
        f"Project: {project or '-'}",
        f"Generated: {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "PRELIMINARY - FOR ENGINEER REVIEW. NOT FOR CONSTRUCTION.",
        "",
        d.project_summary,
        "",
        "1. GEOTECHNICAL REVIEW & WIND",
        f"  Report provided: {'yes' if g.report_provided else 'NO'}",
        f"  Site class (AS 2870): {g.site_classification}   ys: {g.ys}",
        f"  Allowable bearing: {g.bearing_capacity}",
        f"  Founding: {g.founding}",
        f"  Fill: {g.fill}   Groundwater: {g.groundwater}",
        f"  Trees / moisture: {g.trees_or_moisture}",
        f"  Wind class (AS 4055): {g.wind_classification} (source: {g.wind_source})",
        "  Report recommendations:",
        *bullets(g.recommendations),
        "  Concerns:",
        *bullets(g.concerns),
        "",
        "2. ARCHITECTURAL REVIEW",
        f"  Roof: {d.roof_form}; {d.roof_construction}; pitch {d.roof_pitch}; {d.roof_cladding}",
        "  Decks:",
        *bullets(d.deck_areas),
        f"  Walls: ext {d.external_wall_type}; int {d.internal_wall_type}; "
        f"{d.framing_material} to {d.governing_standard}; {d.storeys} storey(s); height {d.wall_height}",
        "  Load paths:",
        *[f"  - {s.element}: carries {s.carries} -> {s.transfers_to}" for s in d.load_paths],
        f"  Footing: {d.footing_system} (site class {d.footing_site_class_used})",
        f"    Basis: {d.footing_as2870_basis}",
        *[f"    - {x}" for x in d.footing_details],
        f"  Wall design / bracing (wind {d.wind_classification_used}):",
        f"    {d.racking_summary}",
        *[f"    - {b.location}: {b.bracing_type} ({b.capacity})" for b in d.bracing_walls],
        f"    Tie-down: {d.tie_down}",
        "    Lintels / beams:",
        *bullets(d.lintels_and_beams),
        "    Articulation / control joints:",
        *bullets(d.articulation_and_control_joints),
        f"  Floor: {d.floor_system} - {d.floor_description}",
        "",
        "ASSUMPTIONS",
        *bullets(d.assumptions),
        "",
        "ENGINEER TO CHECK",
        *bullets(d.engineer_checks),
    ]
    return lines


def build_markup_pdf(
    arch_pdf: bytes,
    project: str,
    geotech: GeotechReview,
    design: DesignAssessment,
    sheets: dict[SheetKind, MarkupSheet],
) -> bytes:
    src = pymupdf.open(stream=arch_pdf, filetype="pdf")
    out = pymupdf.open()

    _write_lines(out, "STRUCTURAL REVIEW SUMMARY", _summary_lines(project, geotech, design))

    for kind in SheetKind:
        sheet = sheets.get(kind)
        if sheet is None:
            continue
        idx = min(max(sheet.source_page_number, 1), src.page_count) - 1
        out.insert_pdf(src, from_page=idx, to_page=idx)
        page = out[-1]
        colour = SHEET_COLOURS[kind]
        fontsize = max(6.0, min(10.0, page.rect.width / 140))
        for m in sheet.marks:
            _add_mark(page, m, colour, fontsize)
        legend = sorted({m.category for m in sheet.marks})
        _banner(page, SHEET_TITLES[kind], colour)
        _notes_panel(page, f"{SHEET_TITLES[kind]} (from arch. page {idx + 1})", colour, legend, sheet.notes)

    return out.tobytes(garbage=3, deflate=True)
