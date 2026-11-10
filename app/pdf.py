"""PDF helpers: render architectural pages for Claude and build the mark-up set."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass

import pymupdf

from . import calcs, office_checks
from .footprint import Footprint
from .elevation import cross_check, snap_joint_elevation
from .elevation import find_openings as find_elevation_openings
from .lintel_check import check_lintels, find_openings
from .snap import (
    BEAM_TAGS,
    FOOTING_TAGS,
    SPAN_TAGS,
    Geometry,
    bracing_length_mm,
    _wall_runs,
    drawing_scale,
    mm_to_pt,
    point_targets,
    snap_bracing,
    snap_members,
    snap_point,
    snap_span,
    snap_span_ends,
    snap_to_wall,
)
from .schemas import (
    SHEET_TITLES,
    Point,
    DesignAssessment,
    Element,
    GeotechReview,
    MarkupSheet,
    ScheduleItem,
    SheetKind,
)

# Claude accepts up to 100 images and 32 MB per request; stay well inside both.
MAX_PAGES = int(os.environ.get("MAX_DRAWING_PAGES", "60"))
MAX_EDGE_PX = 1568  # long edge sent to Claude; keeps detail without oversizing
IMAGE_BUDGET_BYTES = 18 * 1024 * 1024  # total JPEG bytes (~24 MB once base64-encoded)


@dataclass
class PageImage:
    number: int  # 1-based page number in the uploaded set
    width: int
    height: int
    jpeg: bytes
    text: str
    dims: str = ""  # written dimensions with their positions, e.g. "4030@(0.21,0.12)"
    label: str = ""  # where it came from when several sets were uploaded, e.g. "Set B.pdf page 3"
    scale_note: str = ""  # e.g. "1:100 - 1000 mm on site = 0.0238 of the image width ..."
    outline_note: str = ""  # measured building outline on a plan, e.g. "16.3 m across x 8.9 m up the page"


DIM_WORD = re.compile(r"^\d{3,5}$|^\d{1,2}\.\d{2,3}$")  # 4030, 11.25 (levels)


def _dimension_text(page: pymupdf.Page, limit: int = 400) -> str:
    """Written dimensions / levels on the page with their position (normalised to the
    page image, 0-1), so each number can be tied to the wall or roof edge it labels.
    'v' marks text written vertically (a dimension along a vertical line)."""
    r = page.rect
    m = page.rotation_matrix
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            dx, dy = line["dir"]
            vertical = abs(dy) > abs(dx)
            if page.rotation in (90, 270):
                vertical = not vertical
            for span in line["spans"]:
                for word in span["text"].split():
                    if DIM_WORD.match(word):
                        b = span["bbox"]
                        c = pymupdf.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) * m
                        out.append(f"{word}@({c.x / r.width:.2f},{c.y / r.height:.2f}){'v' if vertical else ''}")
                        if len(out) >= limit:
                            return " ".join(out)
    return " ".join(out)


def parse_page_range(spec: str, page_count: int, what: str = "drawing set") -> list[int]:
    """'3-12, 15' -> [3..12, 15] (1-based). Empty -> every page. Raises ValueError."""
    spec = (spec or "").strip()
    if not spec or spec.lower() == "all":
        return list(range(1, page_count + 1))
    spec = re.sub(r"\s*(?:-|–|to)\s*", "-", spec)  # '1 - 2', '1–2', '1 to 2' -> '1-2'
    pages: list[int] = []
    for part in re.split(r"[,;\s]+", spec):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            raise ValueError(f"Could not read page range '{part}'. Use e.g. 3-12, 15.")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        if a < 1 or b > page_count:
            raise ValueError(f"Page range '{part}' is outside the {what} (1-{page_count}).")
        pages += [p for p in range(a, b + 1) if p not in pages]
    if not pages:
        raise ValueError("No drawing pages selected.")
    return sorted(pages)


def format_ranges(pages: list[int]) -> str:
    """[3,4,5,9] -> '3-5, 9'."""
    out, start = [], None
    for i, p in enumerate(pages):
        if start is None:
            start = p
        if i == len(pages) - 1 or pages[i + 1] != p + 1:
            out.append(f"{start}-{p}" if p != start else str(p))
            start = None
    return ", ".join(out)


def extract_pages(pdf_bytes: bytes, pages: list[int]) -> bytes:
    """New PDF holding only the selected (1-based) pages, in order."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as src:
        if pages == list(range(1, src.page_count + 1)):
            return pdf_bytes
        out = pymupdf.open()
        for p in pages:
            out.insert_pdf(src, from_page=p - 1, to_page=p - 1)
        return out.tobytes(garbage=3, deflate=True)


def check_pages(pdf_bytes: bytes, spec: str = "", name: str = "Architectural PDF") -> list[int]:
    """Validate the set and selection up front so problems show at upload time."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        count = doc.page_count
    if count == 0:
        raise ValueError(f"{name} has no pages.")
    pages = parse_page_range(spec, count)
    if len(pages) > MAX_PAGES:
        raise ValueError(
            f"{len(pages)} drawing pages selected; the limit is {MAX_PAGES}. Use 'Drawing pages "
            "to review' to pick the plans, elevations and sections (e.g. 3-12)."
        )
    return pages


def combine_pdfs(parts: list[tuple[str, bytes, list[int]]]) -> tuple[bytes, list[str]]:
    """One PDF holding the chosen pages of each uploaded document, in upload order.
    Returns (pdf, label per page: '<file> page <n>')."""
    if len(parts) == 1 and parts[0][2] == list(range(1, _count(parts[0][1]) + 1)):
        name, data, pages = parts[0]
        return data, [f"{name} page {p}" for p in pages]
    out, labels = pymupdf.open(), []
    for name, data, pages in parts:
        with pymupdf.open(stream=data, filetype="pdf") as src:
            for p in pages:
                out.insert_pdf(src, from_page=p - 1, to_page=p - 1)
                labels.append(f"{name} page {p}")
    return out.tobytes(garbage=3, deflate=True), labels


def _count(data: bytes) -> int:
    with pymupdf.open(stream=data, filetype="pdf") as d:
        return d.page_count


def _scale_note(page: pymupdf.Page) -> str:
    """How long 1000 mm on site is on this page image, so Claude can lay members out at
    their real spacing (bearers at 2200 ctrs, rafters at 900 ctrs ...)."""
    try:
        scale, how = drawing_scale(page)
    except Exception:
        return ""
    if not scale:
        return ""
    paper = 1000 / scale * 72 / 25.4  # points on the page for 1000 mm on site
    return (f"1:{scale} ({how}) - 1000 mm on site = {paper / page.rect.width:.4f} of the image width "
            f"= {paper / page.rect.height:.4f} of its height")


def _outline_note(page: pymupdf.Page) -> str:
    """The building's overall size measured from the wall line work (plans only), so the
    roof span direction isn't guessed: trusses normally span the shorter way."""
    try:
        geo = Geometry(page)
        if not geo.usable:
            return ""
        scale, _ = drawing_scale(page, geo)
        fp = Footprint(geo)
        if not scale or not fp.ok:
            return ""
        xs = [v for h, c, lo, hi, t in fp.walls for v in ((lo, hi) if h else (c,))]
        ys = [v for h, c, lo, hi, t in fp.walls for v in ((c,) if h else (lo, hi))]
        w_mm = (max(xs) - min(xs)) / mm_to_pt(1, scale)
        h_mm = (max(ys) - min(ys)) / mm_to_pt(1, scale)
    except Exception:
        return ""
    if min(w_mm, h_mm) < 4000 or max(w_mm, h_mm) > 60000:
        return ""
    short = "up / down the page" if h_mm < w_mm else "across the page (left / right)"
    return (f"main building walls measure about {w_mm / 1000:.1f} m across the page x {h_mm / 1000:.1f} m "
            f"up / down the page; the shorter direction is {short}")


def render_pages(pdf_bytes: bytes, page_numbers: list[int] | None = None) -> list[PageImage]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    numbers = page_numbers or list(range(1, doc.page_count + 1))
    # Large sets: step resolution / quality down until the images fit the request budget.
    for edge, quality in ((MAX_EDGE_PX, 85), (1400, 80), (1250, 75), (1100, 70), (1000, 65)):
        out = []
        for n in numbers:
            page = doc[n - 1]
            rect = page.rect  # rotated (as displayed) page size in points
            zoom = edge / max(rect.width, rect.height)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            out.append(
                PageImage(
                    number=n,
                    width=pix.width,
                    height=pix.height,
                    jpeg=pix.tobytes("jpeg", jpg_quality=quality),
                    text=page.get_text("text").strip(),
                    dims=_dimension_text(page),
                    scale_note=_scale_note(page),
                    outline_note=_outline_note(page),
                )
            )
        if sum(len(p.jpeg) for p in out) <= IMAGE_BUDGET_BYTES:
            break
    return out


# ---------------------------------------------------------------------------
# Mark-up sheets
#
# House style (from the engineer's own mark-up sets): A3 landscape, address and
# sheet name top-left, classification + notes down the left, the architectural
# plan faded in the middle with colour-coded members, schedule / legend boxes on
# the right and the title block bottom-right. EVERYTHING added here is a PDF
# annotation, so the whole mark-up stays editable in Bluebeam / Acrobat; only the
# faded architectural background is page content.
# ---------------------------------------------------------------------------

A3 = pymupdf.Rect(0, 0, 1190.55, 841.89)
TITLE = pymupdf.Rect(15, 10, 760, 80)
NOTES = pymupdf.Rect(15, 190, 335, 832)
PLAN = pymupdf.Rect(345, 150, 905, 600)
BAND_TOP = pymupdf.Rect(345, 84, 1180, 146)
BAND_RIGHT = pymupdf.Rect(912, 150, 1180, 455)
BAND_BOTTOM = pymupdf.Rect(345, 606, 728, 832)
BAND_BOTTOM_NO_TB = pymupdf.Rect(345, 606, 905, 832)
SCHEDULE = pymupdf.Rect(915, 460, 1180, 676)
SCHEDULE_NO_TB = pymupdf.Rect(915, 460, 1180, 832)
TITLE_BLOCK = pymupdf.Rect(735, 682, 1182, 834)

COMPANY = os.environ.get("TITLE_BLOCK_COMPANY", "ARETE")
COMPANY_SUB = os.environ.get("TITLE_BLOCK_COMPANY_SUB", "ENGINEERING")
AUTHOR = os.environ.get("MARKUP_AUTHOR", "Resi Mark-Up Tool")

RED = (1.0, 0.0, 0.0)
BLUE = (0.0, 0.0, 1.0)
GREEN = (0.0, 0.5, 0.0)
VB_GREEN = (0.0, 0.5, 0.25)
LIGHT_BLUE = (0.0, 0.5, 0.75)
MAGENTA = (1.0, 0.0, 0.5)
ORANGE = (1.0, 0.5, 0.0)
TEAL = (0.0, 0.25, 0.25)
BLACK = (0.0, 0.0, 0.0)
GREY = (0.2, 0.2, 0.2)
BRAND = (0.2, 0.2, 0.95)

# Tag prefix -> colour, matching the engineer's schedules.
TAG_COLOURS = {
    "VB": VB_GREEN, "LD": LIGHT_BLUE, "DS": RED, "LP": ORANGE,
    "EB": RED, "SF": RED, "IB": RED, "TH": RED, "PAD": RED, "P": RED, "F": RED,
    "B": BLUE, "L": BLUE, "J": GREEN, "R": MAGENTA, "C": RED, "S": RED, "T": RED,
    "UP": MAGENTA, "RB": MAGENTA, "HB": MAGENTA, "CJ": RED, "AJ": RED,
}
FOOTING_PREFIXES = {"EB", "SF", "IB", "TH", "PAD", "F"}


def _prefix(tag: str) -> str:
    m = re.match(r"[A-Za-z]+", tag.strip())
    return m.group(0).upper() if m else ""


def tag_colour(tag: str, kind: str = "") -> tuple[float, float, float]:
    if kind == "bracing":
        return TEAL
    p = _prefix(tag)
    if p in TAG_COLOURS:
        return TAG_COLOURS[p]
    if p and p[0] in TAG_COLOURS:
        return TAG_COLOURS[p[0]]
    return GREY


def _info(annot: pymupdf.Annot, subject: str, content: str = "") -> None:
    annot.set_info(title=AUTHOR, subject=subject, content=content)


def _text_width(text: str, fs: float) -> float:
    return pymupdf.get_text_length(text, fontname="helv", fontsize=fs)


def _wrap(text: str, width: float, fs: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words, cur = para.split(), ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if cur and _text_width(trial, fs) > width:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
    return lines


def _text_height(lines: int, fs: float) -> float:
    return lines * fs * 1.18 + 6


def _text(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
    fs: float,
    colour=BLACK,
    subject: str = "Text Box",
    align: int = 0,
    border: float = 0,
    fill=None,
    callout=None,
) -> pymupdf.Annot | None:
    rect = pymupdf.Rect(rect) & A3
    if rect.is_empty or rect.is_infinite or rect.height < fs * 0.8:
        return None
    kw = {}
    if callout:
        kw.update(callout=callout, line_end=pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
    a = page.add_freetext_annot(
        rect, text, fontsize=fs, fontname="Helv", text_color=colour, fill_color=fill,
        border_width=border, align=align, **kw,
    )
    _info(a, subject, text)
    a.update()
    _editor_style(page, a, text, fs, colour, align)
    return a


def _editor_style(page: pymupdf.Page, a: pymupdf.Annot, text: str, fs: float, colour, align: int) -> None:
    """Tell PDF editors the text style to use when the box is clicked for editing.
    PyMuPDF writes only /DA; Bluebeam and Acrobat edit from the default style (/DS) and
    rich text (/RC) and, finding neither, fall back to their own default (12 pt) - so a
    6.5 pt callout jumped to 12 pt when clicked. Never fails the sheet."""
    try:
        doc = page.parent
        rgb = "#%02X%02X%02X" % tuple(round(max(0.0, min(1.0, c)) * 255) for c in (colour or BLACK))
        style = (f"font: Helvetica,sans-serif {fs:g}pt; text-align:{('left', 'center', 'right')[align]}; "
                 f"color:{rgb}")
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
        body = "".join(f"<p>{esc(line) or '&#160;'}</p>" for line in text.split("\n"))
        rc = ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
              'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" xfa:APIVersion="Acrobat:11.0.0" '
              f'xfa:spec="2.0.2" style="{style}">{body}</body>')
        doc.xref_set_key(a.xref, "DS", pymupdf.get_pdf_str(style))
        doc.xref_set_key(a.xref, "RC", pymupdf.get_pdf_str(rc))
    except Exception:
        pass


def _label(page: pymupdf.Page, at: pymupdf.Point, text: str, fs: float, colour, subject: str) -> None:
    w = _text_width(text, fs) + 8
    h = fs * 1.25 + 4
    r = pymupdf.Rect(at.x, at.y - h, at.x + w, at.y) & A3
    if r.is_empty:
        return
    _text(page, r, text, fs, colour, subject)


def _line(page, a, b, colour, width, subject, content="", dashes=None, arrows=False) -> None:
    ln = page.add_line_annot(a, b)
    ln.set_border(width=width, dashes=dashes)
    ln.set_colors(stroke=colour, fill=colour if arrows else None)
    if arrows:
        ln.set_line_ends(pymupdf.PDF_ANNOT_LE_CLOSED_ARROW, pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
    _info(ln, subject, content)
    ln.update()


ARROW_HEAD = 3.0  # pt: span arrowheads (a quarter of the default closed-arrow ends)


def _arrow(page, a, b, colour, width, subject, content="", head: float = ARROW_HEAD) -> None:
    """Double-headed arrow with small open heads, as ONE polyline annotation (so it moves
    and edits as a unit in Bluebeam / Acrobat)."""
    d = b - a
    n = abs(d)
    if n < 1e-6:
        return
    u = d / n
    v = pymupdf.Point(-u.y, u.x)
    h, w = min(head, n / 3), min(head, n / 3) * 0.6
    pts = [a + u * h + v * w, a, a + u * h - v * w, a, b, b - u * h + v * w, b, b - u * h - v * w]
    ln = page.add_polyline_annot(pts)
    ln.set_border(width=width)
    ln.set_colors(stroke=colour)
    _info(ln, subject, content)
    ln.update()


def _rect(page, r, colour, width, subject, fill=None, dashes=None, clouds=-1) -> None:
    a = page.add_rect_annot(r)
    a.set_border(width=width, dashes=dashes, clouds=clouds)
    a.set_colors(stroke=colour, fill=fill)
    _info(a, subject)
    a.update()


# ---------------------------------------------------------------------------
# Frame: title, notes, title block
# ---------------------------------------------------------------------------


def _title(page, project: str, sheet_title: str) -> None:
    _text(page, pymupdf.Rect(TITLE.x0, TITLE.y0, TITLE.x1, TITLE.y0 + 40), (project or "PROJECT").upper(), 30, BLACK, "Title")
    _text(page, pymupdf.Rect(TITLE.x0, TITLE.y0 + 40, TITLE.x1, TITLE.y1), sheet_title, 20, BLACK, "Title")


def _notes(page, classification: str, notes: list[str]) -> list[str]:
    """Classification + notes column. Returns notes that did not fit (never dropped)."""
    y = NOTES.y0
    if classification:
        cls = classification.upper()
        for fs in (10, 9, 8, 7):
            lines = _wrap(cls, NOTES.width - 8, fs)
            if len(lines) <= 4:
                break
        lines = lines[:4]
        h = _text_height(len(lines), fs)
        _text(page, pymupdf.Rect(NOTES.x0, y, NOTES.x1, y + h), "\n".join(lines), fs, BLACK, "Classification")
        y += h + 8
    notes = [n if n.lstrip().startswith("-") else f"-{n}" for n in notes]
    if not notes:
        return []
    avail = NOTES.y1 - y
    for fs in (8.5, 8, 7.5, 7, 6.5, 6, 5.5, 5):
        lines = _wrap("NOTES:\n\n" + "\n\n".join(notes), NOTES.width - 8, fs)
        if _text_height(len(lines), fs) <= avail:
            _text(page, pymupdf.Rect(NOTES.x0, y, NOTES.x1, y + _text_height(len(lines), fs)), "\n".join(lines), fs, BLACK, "Notes")
            return []
    # Still too long at the smallest size: place as many whole notes as fit.
    fs, kept = 5, []
    for n in notes:
        lines = _wrap("NOTES:\n\n" + "\n\n".join(kept + [n]), NOTES.width - 8, fs)
        if _text_height(len(lines), fs) > avail:
            break
        kept.append(n)
    if kept:
        lines = _wrap("NOTES:\n\n" + "\n\n".join(kept), NOTES.width - 8, fs)
        _text(page, pymupdf.Rect(NOTES.x0, y, NOTES.x1, y + _text_height(len(lines), fs)), "\n".join(lines), fs, BLACK, "Notes")
    return notes[len(kept):]


def _title_block(page, project: str, dwg_title: str, dwg_no: str, job_no: str, rev: str, made_by: str, date: str) -> None:
    tb = TITLE_BLOCK
    split_y, logo_x = tb.y0 + 92, tb.x0 + 160
    _rect(page, tb, BLACK, 1.5, "Title Block")
    _line(page, (tb.x0, split_y), (tb.x1, split_y), BLACK, 1, "Title Block")
    _line(page, (logo_x, tb.y0), (logo_x, split_y), BLACK, 1, "Title Block")
    rows = [tb.y0, tb.y0 + 30, tb.y0 + 61, split_y]
    for y in rows[1:3]:
        _line(page, (logo_x, y), (tb.x1, y), BLACK, 0.75, "Title Block")
    lab_x = logo_x + 68
    _line(page, (lab_x, tb.y0), (lab_x, split_y), BLACK, 0.75, "Title Block")
    mid_y = split_y + (tb.y1 - split_y) / 2
    _line(page, (tb.x0, mid_y), (tb.x1, mid_y), BLACK, 0.75, "Title Block")
    _line(page, (tb.x0 + 95, split_y), (tb.x0 + 95, tb.y1), BLACK, 0.75, "Title Block")

    # Company
    _text(page, pymupdf.Rect(tb.x0 + 6, tb.y0 + 18, logo_x - 4, tb.y0 + 52), COMPANY, 26, BRAND, "Title Block", align=1)
    _text(page, pymupdf.Rect(tb.x0 + 6, tb.y0 + 52, logo_x - 4, tb.y0 + 70), COMPANY_SUB, 9, BRAND, "Title Block", align=1)

    def cell(x0, y0, x1, y1, text, fs=10.5):
        _text(page, pymupdf.Rect(x0 + 3, y0 + 4, x1 - 2, y1 - 1), text, fs, BLACK, "Title Block")

    cell(logo_x, rows[0], lab_x, rows[1], "Job No", 11)
    cell(lab_x, rows[0], tb.x1, rows[1], job_no or " ")
    cell(logo_x, rows[1], lab_x, rows[2], "Dwg No", 11)
    rev_x = tb.x1 - 70
    _line(page, (rev_x, rows[1]), (rev_x, rows[2]), BLACK, 0.75, "Title Block")
    _line(page, (rev_x + 38, rows[1]), (rev_x + 38, rows[2]), BLACK, 0.75, "Title Block")
    cell(lab_x, rows[1], rev_x, rows[2], dwg_no)
    cell(rev_x, rows[1], rev_x + 38, rows[2], "REV", 9)
    cell(rev_x + 38, rows[1], tb.x1, rows[2], rev or "A")
    cell(logo_x, rows[2], lab_x, rows[3], "Date", 11)
    mb_x = tb.x1 - 110
    _line(page, (mb_x, rows[2]), (mb_x, rows[3]), BLACK, 0.75, "Title Block")
    _line(page, (mb_x + 60, rows[2]), (mb_x + 60, rows[3]), BLACK, 0.75, "Title Block")
    cell(lab_x, rows[2], mb_x, rows[3], date)
    cell(mb_x, rows[2], mb_x + 60, rows[3], "Made by", 9)
    cell(mb_x + 60, rows[2], tb.x1, rows[3], made_by or " ")
    cell(tb.x0, split_y, tb.x0 + 95, mid_y, "Job Title", 11)
    cell(tb.x0 + 95, split_y, tb.x1, mid_y, (project or " ").upper())
    cell(tb.x0, mid_y, tb.x0 + 95, tb.y1, "Dwg Title", 11)
    cell(tb.x0 + 95, mid_y, tb.x1, tb.y1, dwg_title)


def _schedule(page, title: str, items: list[ScheduleItem], legend_title: str, legend: list[ScheduleItem], box=SCHEDULE,
              masonry: bool = False) -> None:
    entries: list[tuple[str, tuple, bool, str]] = []  # text, colour, is_heading, legend tag
    if items:
        entries.append((title or "SCHEDULE:", BLACK, True, ""))
        entries += [(f"{i.tag}: {i.text}", tag_colour(i.tag), False, "") for i in items]
    if legend:
        if entries:
            entries.append(("", BLACK, False, ""))
        entries.append((legend_title or "LEGEND:", BLACK, True, ""))
        entries += [(i.text, TEAL, False, i.tag) for i in legend]
    if masonry:  # brick veneer / masonry: the office steel lintel schedule, as its own section
        if entries:
            entries.append(("", BLACK, False, ""))
        entries.append(("MASONRY LINTEL SCHEDULE:", BLACK, True, ""))
        entries.append(("SPAN - STEEL SIZE (GALVANISED)", RED, False, ""))
        entries += [(f"{span}: {size}", RED, False, "") for span, size in MASONRY_LINTELS]
        entries.append((MASONRY_LINTEL_NOTE, RED, False, ""))
    if not entries:
        return
    inner = box.width - 12
    for fs in (10, 9, 8.5, 8, 7.5, 7, 6.5, 6, 5.5, 5):
        blocks = []
        for text, colour, head, ltag in entries:
            width = inner - (52 if ltag else 0)
            lines = _wrap(text, width, fs) if text else [""]
            height = _text_height(max(len(lines), 2 if ltag else 1), fs) - 4
            blocks.append((lines, colour, head, ltag, height))
        if sum(b[4] for b in blocks) + 10 <= box.height:
            break
    used = sum(b[4] for b in blocks) + 12
    _rect(page, pymupdf.Rect(box.x0, box.y0, box.x1, min(box.y1, box.y0 + used)), BLACK, 1, "Schedule")
    y = box.y0 + 5
    for lines, colour, head, ltag, h in blocks:
        if lines != [""]:
            x0 = box.x0 + 6
            if ltag:  # bracing legend: sample line + type label
                dashed = ltag.strip().lower().startswith("d")
                line_y = y + fs * 1.25 + 2
                _line(page, (x0, line_y), (x0 + 44, line_y), TEAL, 1.5, "Legend", ltag, dashes=[3, 3] if dashed else None)
                _text(page, pymupdf.Rect(x0 + 8, y - 2, x0 + 44, line_y), ltag, fs, TEAL, "Legend")
                x0 += 52
            _text(page, pymupdf.Rect(x0, y - 2, box.x1 - 4, y + h + 2), "\n".join(lines), fs, colour, "Schedule" if not head else "Schedule Heading")
        y += h


# ---------------------------------------------------------------------------
# Plan background and coordinate mapping
# ---------------------------------------------------------------------------


def _fit_region(sheet: MarkupSheet, margin: float = 0.02) -> int:
    """Enlarge sheet.plan_region to take in mark-ups Claude placed outside it (otherwise
    they would be silently left off the sheet). Returns how many were outside."""
    r = sheet.plan_region
    x1, x2 = sorted((r.x1, r.x2))
    y1, y2 = sorted((r.y1, r.y2))
    if x2 - x1 < 0.1 or y2 - y1 < 0.1:
        return 0  # PlanMap uses the whole page anyway
    pts = [p for e in sheet.elements for p in ((e.start, e.end) if e.kind != "callout" else (e.start,))]
    out = [p for p in pts if not (x1 - margin <= p.x <= x2 + margin and y1 - margin <= p.y <= y2 + margin)]
    if not out:
        return 0
    xs, ys = [p.x for p in pts] + [x1, x2], [p.y for p in pts] + [y1, y2]
    sheet.plan_region = r.model_copy(update={
        "x1": max(0.0, min(xs) - margin), "y1": max(0.0, min(ys) - margin),
        "x2": min(1.0, max(xs) + margin), "y2": min(1.0, max(ys) + margin)})
    return len({id(e) for e in sheet.elements for p in (e.start, e.end) if any(p is q for q in out)})


class PlanMap:
    """Maps normalised source-image coordinates onto the sheet."""

    def __init__(self, src_page: pymupdf.Page, region) -> None:
        r = src_page.rect
        x1, x2 = sorted((min(max(region.x1, 0), 1), min(max(region.x2, 0), 1)))
        y1, y2 = sorted((min(max(region.y1, 0), 1), min(max(region.y2, 0), 1)))
        if x2 - x1 < 0.1 or y2 - y1 < 0.1:  # implausible region: use the whole page
            x1, y1, x2, y2 = 0, 0, 1, 1
        self.src_w, self.src_h = r.width, r.height
        self.clip = pymupdf.Rect(x1 * r.width, y1 * r.height, x2 * r.width, y2 * r.height)
        s = min(PLAN.width / self.clip.width, PLAN.height / self.clip.height)
        w, h = self.clip.width * s, self.clip.height * s
        self.scale = s
        self.placed = pymupdf.Rect(
            PLAN.x0 + (PLAN.width - w) / 2, PLAN.y0 + (PLAN.height - h) / 2,
            PLAN.x0 + (PLAN.width + w) / 2, PLAN.y0 + (PLAN.height + h) / 2,
        )
        self.src_page = src_page
        self.mm_pt: float | None = None  # sheet points per real mm (set when the plan scale is known)
        self.specs: dict[str, str] = {}  # schedule text by tag
        self.bands: dict[int, list[tuple]] = {}  # footing lines by id(element), see _footing_bands
        # Everything drawn on the plan, so tags can be placed clear of it (see _place_labels).
        self.segments: list[tuple] = []  # (a, b) of every line / arrow drawn
        self.boxes: list[pymupdf.Rect] = []  # piers, columns, clouds ...
        self.label_requests: list[tuple] = []  # (shape, geometry, text, fs, colour, subject)

    def spec(self, tag: str) -> str:
        return self.specs.get((tag or "").strip().upper(), "")

    def __call__(self, pt) -> pymupdf.Point:
        px = min(max(pt.x, 0), 1) * self.src_w
        py = min(max(pt.y, 0), 1) * self.src_h
        return pymupdf.Point(
            self.placed.x0 + (px - self.clip.x0) * self.scale,
            self.placed.y0 + (py - self.clip.y0) * self.scale,
        )

    def inside(self, pt, margin: float = 0.02) -> bool:
        x, y = pt.x * self.src_w, pt.y * self.src_h
        mx, my = margin * self.src_w, margin * self.src_h
        c = self.clip
        return c.x0 - mx <= x <= c.x1 + mx and c.y0 - my <= y <= c.y1 + my

    def draw_background(self, page: pymupdf.Page, src_doc: pymupdf.Document) -> None:
        sp = self.src_page
        clip = (self.clip * sp.derotation_matrix).normalize()
        page.show_pdf_page(self.placed, src_doc, sp.number, clip=clip, rotate=-sp.rotation)
        # Fade the architectural drawing so the mark-up reads clearly.
        page.draw_rect(self.placed, color=None, fill=(1, 1, 1), fill_opacity=0.45, overlay=True)


# ---------------------------------------------------------------------------
# Plan elements
# ---------------------------------------------------------------------------


WHITE = (1.0, 1.0, 1.0)
TAG_FS = float(os.environ.get("MARKUP_TAG_FONT_SIZE", "9"))  # tags on the plan (P1, EB1, L3 ...)


def _seg_hits_rect(p: pymupdf.Point, q: pymupdf.Point, r: pymupdf.Rect) -> bool:
    """Does the segment p-q pass through rect r (Liang-Barsky clip)?"""
    t0, t1 = 0.0, 1.0
    dx, dy = q.x - p.x, q.y - p.y
    for pk, qk in ((-dx, p.x - r.x0), (dx, r.x1 - p.x), (-dy, p.y - r.y0), (dy, r.y1 - p.y)):
        if abs(pk) < 1e-9:
            if qk < 0:
                return False
            continue
        t = qk / pk
        if pk < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _label_rect(text: str, fs: float, centre: pymupdf.Point) -> pymupdf.Rect:
    """Tight box for a tag: the text's width and line height plus a small margin."""
    w = _text_width(text, fs) + 3.5
    h = fs * 1.18 + 1.5
    return pymupdf.Rect(centre.x - w / 2, centre.y - h / 2, centre.x + w / 2, centre.y + h / 2)


def _place_labels(page: pymupdf.Page, m: "PlanMap") -> int:
    """Draw every tag (P1, EB1, L3 ...) in a tight box with a white background, in the first
    nearby spot that doesn't overlap another tag, a member line, an arrow, a pier, a column or
    a cloud. Tags are placed after everything else is drawn so they can see all of it. A tag
    with no clear spot is left out if the same tag is already shown nearby on the sheet;
    otherwise it goes where it overlaps least. Returns how many were left out."""
    placed: list[pymupdf.Rect] = []
    shown: dict[str, list[pymupdf.Point]] = {}
    left_out = 0
    area = m.placed  # the plan's area on the sheet

    def hits(r: pymupdf.Rect) -> int:
        pad = r + (-0.6, -0.6, 0.6, 0.6)
        n = sum(1 for o in placed if pad.intersects(o))
        n += sum(1 for o in m.boxes if pad.intersects(o))
        n += sum(1 for p, q in m.segments if _seg_hits_rect(p, q, pad))
        return n

    def candidates(shape, geo, text, fs):
        w0 = _label_rect(text, fs, pymupdf.Point(0, 0))
        hw, hh = w0.width / 2, w0.height / 2
        if shape == "line":
            a, b = geo
            d = b - a
            length = abs(d) or 1.0
            u = d / length
            n = pymupdf.Point(-u.y, u.x)
            if n.y > 0 or (abs(n.y) < 1e-9 and n.x < 0):
                n = n * -1  # try above / right of the line first
            ext = abs(n.x) * hw + abs(n.y) * hh
            for t in (0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9):
                base = a + d * t
                for gap in (1.5, 5.0, 10.0, 18.0, 28.0):
                    for side in (1, -1):
                        yield base + n * (side * (ext + gap))
        elif shape == "point":
            c, r = geo
            dirs = [(1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1)]
            for gap in (1.5, 5.0, 10.0, 16.0, 24.0, 34.0):
                for dx, dy in dirs:
                    v = pymupdf.Point(dx, dy)
                    v = v / abs(v)
                    ext = abs(v.x) * hw + abs(v.y) * hh
                    yield c + v * (r + gap + ext)
        else:  # box (cloud): just outside one of its sides
            r = geo
            for gap in (2.0, 6.0, 12.0):
                yield pymupdf.Point(r.x1 + gap + hw, r.y0 + hh)
                yield pymupdf.Point(r.x0 + hw, r.y0 - gap - hh)
                yield pymupdf.Point(r.x0 - gap - hw, r.y0 + hh)
                yield pymupdf.Point(r.x0 + hw, r.y1 + gap + hh)

    for shape, geo, text, fs, colour, subject in m.label_requests:
        best, best_hits = None, None
        for c in candidates(shape, geo, text, fs):
            r = _label_rect(text, fs, c)
            if not area.contains(r):
                continue
            n = hits(r)
            if n == 0:
                best, best_hits = r, 0
                break
            if best_hits is None or n < best_hits:
                best, best_hits = r, n
        if best is None:
            continue
        anchor = geo[0] if shape == "point" else ((geo[0] + geo[1]) * 0.5 if shape == "line" else geo.tl)
        if best_hits and any(abs(anchor - p) < 120 for p in shown.get(text, [])):
            left_out += 1  # no clear spot, and the same tag is already shown close by
            continue
        _text(page, best, text, fs, colour, subject, align=1, fill=WHITE)
        placed.append(best)
        # Moved away from what it names: a short leader line back to it.
        if shape == "line":
            a_, b_ = geo
            d_ = b_ - a_
            t_ = max(0.0, min(1.0, ((best.tl + best.br) * 0.5 - a_).x * d_.x / max(1e-6, d_.x ** 2 + d_.y ** 2)
                               + ((best.tl + best.br) * 0.5 - a_).y * d_.y / max(1e-6, d_.x ** 2 + d_.y ** 2)))
            target = a_ + d_ * t_
        elif shape == "point":
            c_, r_ = geo
            v_ = (best.tl + best.br) * 0.5 - c_
            target = c_ + (v_ / abs(v_)) * r_ if abs(v_) > 1e-6 else c_
        else:
            target = pymupdf.Point(min(max((best.x0 + best.x1) / 2, geo.x0), geo.x1),
                                   min(max((best.y0 + best.y1) / 2, geo.y0), geo.y1))
        near = pymupdf.Point(min(max(target.x, best.x0), best.x1), min(max(target.y, best.y0), best.y1))
        # (no leader through a callout ring, pier or column - the tag sits close enough)
        if abs(near - target) > 6 and not any(_seg_hits_rect(near, target, o + (-0.5, -0.5, 0.5, 0.5))
                                              for o in m.boxes if not o.contains(target)):
            _line(page, near, target, colour, 0.5, "Tag Leader")
        shown.setdefault(text, []).append(anchor)
    return left_out


def _offset_label_point(a: pymupdf.Point, b: pymupdf.Point, dist: float = 4) -> pymupdf.Point:
    mid = pymupdf.Point((a.x + b.x) / 2, (a.y + b.y) / 2)
    horizontal = abs(b.x - a.x) >= abs(b.y - a.y)
    return pymupdf.Point(mid.x - 8, mid.y - dist) if horizontal else pymupdf.Point(mid.x + dist, mid.y + 6)


# Real sizes of piers / columns, read from the schedule or the element's own text, so the
# symbols are drawn roughly to scale on the plan (e.g. a 450 dia pier at 1:100).
_DIA = re.compile(r"(\d{3,4})\s*(?:MM\s*)?(?:DIA\b|DIAM|Ø)", re.I)
_SQUARE = re.compile(r"(\d{2,3})\s*[xX×]\s*(\d{2,3})(?=\s*(?:[xX×]\s*[\d.]+\s*)?(?:SHS|RHS|CHS|POST|BRICK|MASONRY|BLOCK|COLUMN|PIER|F\d|TP|HW|MGP|LVL|GALV))", re.I)
_SHS = re.compile(r"(\d{2,3})\s*[xX×]\s*\d{1,2}(?:\.\d)?\s*(?:SHS|CHS|RHS)", re.I)
PIER_DEFAULT_MM = 450.0


def _sizes(text: str) -> tuple[float | None, float | None]:
    """(bored pier diameter mm, square / post side mm) found in a schedule or note."""
    d = _DIA.search(text or "")
    sq = _SQUARE.search(text or "")
    shs = _SHS.search(text or "")  # '89x3.5 SHS': size x wall thickness
    side = float(max(sq.group(1), sq.group(2), key=int)) if sq else (float(shs.group(1)) if shs else None)
    return (float(d.group(1)) if d else None), side


_WIDTH = re.compile(r"(\d{3,4})\s*(?:MM\s*)?(?:W\b|WIDE)", re.I)


def _footing_width_pt(e: Element, m: PlanMap) -> float:
    m_ = _WIDTH.search(f"{e.text} {m.spec(e.tag)}")
    return _scaled(m, (float(m_.group(1)) if m_ else 450.0) / 2, 4.0, 2.0, 10.0)


def _cross(p1, p2, q1, q2):
    """Intersection of the infinite lines p1-p2 and q1-q2 (None if parallel)."""
    d1, d2 = p2 - p1, q2 - q1
    den = d1.x * d2.y - d1.y * d2.x
    if abs(den) < 1e-6:
        return None
    t = ((q1.x - p1.x) * d2.y - (q1.y - p1.y) * d2.x) / den
    return p1 + d1 * t


def _footing_house_style(sheet: MarkupSheet, page: pymupdf.Page, title: str) -> list[str]:
    """Footing sheets as the engineers draw them, whatever Claude returned:
    - an untagged member (Claude sometimes leaves the footing runs untagged, so they were
      drawn as plain grey single lines) is tagged from the schedule: on the building's
      perimeter it is the edge beam / strip footing (EB / SF), inside it the internal beam (IB);
    - a cloud drawn round the slab (covering a large part of the plan) is replaced by the
      slab's single diagonal line, tagged S1 (or the schedule's slab tag)."""
    tags = [s.tag.strip().upper() for s in sheet.schedule]
    edge = next((t for t in tags if _prefix(t) in ("EB", "SF")), "EB1")
    internal = next((t for t in tags if _prefix(t) == "IB"), None)
    specs = {s.tag.strip().upper(): s.text.upper() for s in sheet.schedule}
    slab = next((t for t in tags if _prefix(t) == "S" and "SLAB" in specs.get(t, "")), "S1")
    out = []
    w, h = page.rect.width, page.rect.height
    untagged = [i for i, e in enumerate(sheet.elements) if e.kind == "member" and not e.tag.strip()]
    if untagged:
        segs = [(pymupdf.Point(e.start.x * w, e.start.y * h), pymupdf.Point(e.end.x * w, e.end.y * h))
                for e in sheet.elements
                if e.kind == "member" and (not e.tag.strip() or _prefix(e.tag) in ("EB", "SF", "IB"))]

        def ray_hits(p: pymupdf.Point, d: pymupdf.Point) -> bool:
            for q1, q2 in segs:
                e_ = q2 - q1
                den = d.x * e_.y - d.y * e_.x
                if abs(den) < 1e-9:
                    continue
                t = ((q1.x - p.x) * e_.y - (q1.y - p.y) * e_.x) / den
                s_ = ((q1.x - p.x) * d.y - (q1.y - p.y) * d.x) / den
                if t > 0 and -0.02 <= s_ <= 1.02:
                    return True
            return False

        def open_to_outside(p: pymupdf.Point) -> bool:
            """A point outside the footing outline can see out past every footing line in at
            least one direction (a recess is open on one side); inside, every way is blocked."""
            return any(not ray_hits(p, d) for d in (pymupdf.Point(1, 0), pymupdf.Point(-1, 0),
                                                       pymupdf.Point(0, 1), pymupdf.Point(0, -1)))

        named = {edge: 0, (internal or "IB1"): 0}
        for i in untagged:
            e = sheet.elements[i]
            a, b = pymupdf.Point(e.start.x * w, e.start.y * h), pymupdf.Point(e.end.x * w, e.end.y * h)
            if abs(b - a) < 1:
                continue
            u = (b - a) / abs(b - a)
            n = pymupdf.Point(-u.y, u.x)
            perimeter = False
            for f in (0.25, 0.5, 0.75):  # a few points along it (a wall can be partly hidden)
                p = a + (b - a) * f
                if open_to_outside(p + n * 3) or open_to_outside(p - n * 3):
                    perimeter = True
                    break
            tag = edge if perimeter else (internal or "IB1")
            sheet.elements[i] = e.model_copy(update={"tag": tag})
            named[tag] = named.get(tag, 0) + 1
        done = ", ".join(f"{n} as {t}" for t, n in named.items() if n)
        if done:
            out.append(f"{title}: {len(untagged)} untagged footing line(s) were tagged from their position ({done}) - check")
    r = sheet.plan_region
    plan_area = max(1e-6, abs((r.x2 - r.x1) * (r.y2 - r.y1)))
    big = [i for i, e in enumerate(sheet.elements) if e.kind == "cloud"
           and abs((e.end.x - e.start.x) * (e.end.y - e.start.y)) > 0.2 * plan_area]
    if big:
        has_slab_line = any(e.kind == "member" and _prefix(e.tag) == "S" for e in sheet.elements)
        first = sheet.elements[big[0]]
        keep = [e for i, e in enumerate(sheet.elements) if i not in big]
        if not has_slab_line:
            x0_, x1_ = sorted((first.start.x, first.end.x))
            y0_, y1_ = sorted((first.start.y, first.end.y))
            keep.append(first.model_copy(update={"kind": "member", "tag": slab, "text": first.text,
                                                 "start": Point(x=x0_, y=y1_), "end": Point(x=x1_, y=y0_)}))
        sheet.elements = keep
        out.append(f"{title}: slab cloud(s) replaced by the slab's diagonal line ({slab}), as the engineers draw it")
    return out


def _footing_bands(sheet: MarkupSheet, m: PlanMap) -> dict[int, list[tuple]]:
    """The lines for every strip footing / edge beam / internal beam, as the engineers draw
    them: two lines at the footing's width (to scale). Perimeter footings (SF / EB) have a
    solid line on the outside face and a dashed line on the inside face - which side is
    inside is found from the whole footing outline, so recesses come out right - and each
    corner is closed by meeting the neighbouring footing's lines. Internal beams (IB) are
    two dashed lines. Returns {id(element): [(a, b, dashed), ...]}."""
    segs = []
    for e in sheet.elements:
        if e.kind == "member" and _prefix(e.tag) in ("SF", "EB", "IB"):
            a, b = m(e.start), m(e.end)
            if abs(b - a) > 1:
                segs.append((e, a, b, _footing_width_pt(e, m)))
    perim = [(a, b) for e, a, b, _ in segs if _prefix(e.tag) in ("SF", "EB")]

    def inside(pt):
        n = 0
        for p, q in perim:
            if (p.y > pt.y) != (q.y > pt.y) and p.x + (pt.y - p.y) * (q.x - p.x) / (q.y - p.y) > pt.x:
                n += 1
        return n % 2 == 1

    cx = sum(p.x + q.x for p, q in perim) / (2 * len(perim)) if perim else 0
    cy = sum(p.y + q.y for p, q in perim) / (2 * len(perim)) if perim else 0
    info = {}
    for e, a, b, half in segs:
        u = (b - a) / abs(b - a)
        n = pymupdf.Point(-u.y, u.x)
        mid = (a + b) * 0.5
        side_in = [inside(mid + n * half * 1.5), inside(mid - n * half * 1.5)]
        if side_in[0] != side_in[1]:
            inward = n if side_in[0] else n * -1
        else:  # open or odd outline: fall back to the outline's middle
            inward = n if ((pymupdf.Point(cx, cy) - mid).x * n.x + (pymupdf.Point(cx, cy) - mid).y * n.y) >= 0 else n * -1
        info[id(e)] = (e, a, b, half, inward)

    out: dict[int, list[tuple]] = {}
    ib_keys = []
    for key, (e, a, b, half, inward) in info.items():
        if _prefix(e.tag) == "IB":
            out[key] = [(a + inward * half, b + inward * half, True), (a - inward * half, b - inward * half, True)]
            ib_keys.append(key)
            continue
        lines = []
        for sign, dashed in ((-1, False), (1, True)):  # outside face solid, inside face dashed
            off = inward * (half * sign)
            p, q = a + off, b + off
            ends = []
            for end in (a, b):
                new = None
                for k2, (e2, a2, b2, half2, in2) in info.items():
                    if k2 == key or _prefix(e2.tag) not in ("SF", "EB"):
                        continue
                    # A neighbour is any footing whose line passes close to this end (after
                    # snapping, corners rarely meet exactly).
                    d2 = b2 - a2
                    t = max(0.0, min(1.0, ((end - a2).x * d2.x + (end - a2).y * d2.y) / max(1e-6, d2.x ** 2 + d2.y ** 2)))
                    if abs(end - (a2 + d2 * t)) > max(6.0, 2.5 * max(half, half2)):
                        continue
                    off2 = in2 * (half2 * sign)
                    x = _cross(p, q, a2 + off2, b2 + off2)
                    if x is not None:
                        new = x
                        break
                ends.append(new)
            lines.append((ends[0] or p, ends[1] or q, dashed))
        out[key] = lines
    # Internal beams stop at the inside face of the perimeter footing (as drawn by the engineers).
    inner = [l for k, ls in out.items() if k not in ib_keys for l in ls if l[2]]
    for key in ib_keys:
        half = info[key][3]
        trimmed = []
        for p, q, dashed in out[key]:
            ends = [p, q]
            for i, end in enumerate((p, q)):
                best = None
                for a2, b2, _ in inner:
                    x = _cross(p, q, a2, b2)
                    if x is None:
                        continue
                    d2 = b2 - a2
                    t = ((x - a2).x * d2.x + (x - a2).y * d2.y) / max(1e-6, d2.x ** 2 + d2.y ** 2)
                    if -0.05 <= t <= 1.05 and abs(x - end) <= 3 * half + 4 and (best is None or abs(x - end) < abs(best - end)):
                        best = x
                if best is not None:
                    ends[i] = best
            trimmed.append((ends[0], ends[1], dashed))
        out[key] = trimmed
    return out

def _scaled(m: PlanMap, mm: float, fallback: float, lo: float, hi: float) -> float:
    """mm on site -> points on the sheet at the plan's scale, kept legible (lo..hi)."""
    return fallback if not m.mm_pt else min(hi, max(lo, mm * m.mm_pt))




def _slab_line(page, e: Element, a: pymupdf.Point, b: pymupdf.Point, colour, m: "PlanMap") -> None:
    """The slab as the engineers mark it: a line across the slab, corner to corner, with an
    arrow at each end, and the slab tag (S1, S2 - the schedule gives the detail) at the
    middle of the line in a tight circle with a white background."""
    _arrow(page, a, b, colour, 1.25, "Slab", e.text or e.tag, head=4.0)
    m.segments.append((a, b))
    spec = (e.text or m.spec(e.tag) or "CONCRETE SLAB").strip().upper()
    label = e.tag.strip().upper() or "SLAB"  # the schedule gives the detail
    w, h = _text_width(label, TAG_FS) + 2, TAG_FS * 1.18 + 1
    mid = (a + b) * 0.5
    r = max(w, h) / 2 + 1.5  # a circle just clear of the tag
    ring = pymupdf.Rect(mid.x - r, mid.y - r, mid.x + r, mid.y + r)
    c = page.add_circle_annot(ring)
    c.set_border(width=1.0)
    c.set_colors(stroke=colour, fill=WHITE)
    _info(c, "Slab Callout", f"{label}: {spec}" if not spec.startswith(label) else spec)
    c.update()
    _text(page, pymupdf.Rect(mid.x - w / 2, mid.y - h / 2, mid.x + w / 2, mid.y + h / 2), label,
          TAG_FS, colour, "Slab Callout", align=1)
    m.boxes.append(ring)


def _reentrant_bars(page, sheet: MarkupSheet, m: "PlanMap") -> int:
    """Re-entrant bars at every re-entrant (inside) corner of the slab outline, as the
    engineers draw them: 3 short parallel bars across the corner at 45 degrees, 2000 long,
    100 apart, the first 40 from the corner, plus a note. Only on slab sheets. Returns the
    number of corners marked."""
    tags = {s.tag.strip().upper(): s.text.upper() for s in sheet.schedule}
    slab = any(e.kind == "member" and _prefix(e.tag) == "S" for e in sheet.elements) or \
        any("SLAB" in t for k, t in tags.items() if _prefix(k) == "S")
    if not slab:
        return 0
    segs = [(m(e.start), m(e.end), e) for e in sheet.elements
            if e.kind == "member" and _prefix(e.tag) in ("SF", "EB") and abs(m(e.end) - m(e.start)) > 1]
    if len(segs) < 4:
        return 0
    # Keep the closed slab outline only: drop runs with a free end (a porch / paving strip
    # or a stub that butts into the outline part-way along an edge), repeatedly, as they
    # throw out the inside / outside test.
    outline = segs
    while True:
        ends = [pt for p, q, _ in outline for pt in (p, q)]

        def joined(pt):
            return sum(abs(pt - o) <= 6 for o in ends) >= 2  # itself and another run

        kept = [s for s in outline if joined(s[0]) and joined(s[1])]
        if len(kept) == len(outline):
            break
        outline = kept
    if len(outline) >= 4:
        segs = outline

    def inside(pt):
        n = 0
        for p, q, _ in segs:
            if (p.y > pt.y) != (q.y > pt.y) and p.x + (pt.y - p.y) * (q.x - p.x) / (q.y - p.y) > pt.x:
                n += 1
        return n % 2 == 1

    def unit(v):
        return v / abs(v)

    corners = []
    for i, (p1, q1, e1) in enumerate(segs):
        for p2, q2, e2 in segs[i + 1:]:
            if abs(abs((unit(q1 - p1).x * unit(q2 - p2).x + unit(q1 - p1).y * unit(q2 - p2).y)) - 1) < 0.05:
                continue  # parallel
            c = _cross(p1, q1, p2, q2)
            if c is None or min(abs(c - p1), abs(c - q1)) > 6 or min(abs(c - p2), abs(c - q2)) > 6:
                continue
            # Look into the four quadrants round the corner: slab in three of them means a
            # re-entrant (inside) corner; the bars go across it, facing away from the gap.
            u1, u2 = unit(q1 - p1), unit(q2 - p2)
            quads = [(s1, s2, inside(c + (u1 * s1 + u2 * s2) * 8)) for s1 in (1, -1) for s2 in (1, -1)]
            if sum(q[2] for q in quads) != 3:
                continue
            s1, s2, _ = next(q for q in quads if not q[2])
            bis = unit((u1 * s1 + u2 * s2) * -1)
            if all(abs(c - k[0]) > 10 for k in corners):
                corners.append((c, bis, e1))
    if not corners:
        return 0
    half_len = _scaled(m, 1000, 28.0, 14.0, 60.0)
    first = _scaled(m, 40, 1.5, 1.0, 4.0)
    gap = _scaled(m, 100, 2.5, 1.6, 6.0)
    for c, bis, e in corners:
        t = pymupdf.Point(-bis.y, bis.x)
        footing_half = _footing_width_pt(e, m)
        for k in range(3):
            centre = c + bis * (footing_half + first + k * gap)
            p, q = centre - t * half_len, centre + t * half_len
            _line(page, p, q, RED, 1.0, "Re-entrant Bars", "3/N12 2000 LONG RE-ENTRANT BARS")
            m.segments.append((p, q))
        m.label_requests.append(("line", (c + bis * (footing_half + first) - t * half_len,
                                          c + bis * (footing_half + first) + t * half_len),
                                 "3/N12", TAG_FS, RED, "Re-entrant Bars Tag"))
    note = ("-3/N12 2000 LONG RE-ENTRANT BARS TIED OVER SLAB MESH AT EACH RE-ENTRANT CORNER. "
            "100 BETWEEN BARS AND FIRST BAR 40 FROM CORNER.")
    if not any("RE-ENTRANT" in n.upper() for n in sheet.notes):
        sheet.notes.append(note)
    return len(corners)


EXTERNAL_SLAB = re.compile(r"PORCH|PAVING|PATIO|ALFRESCO|VERANDAH|TERRACE|CARPORT|ENTRY SLAB|PATH", re.I)


def _external_slab_note(sheet: MarkupSheet) -> bool:
    """Office practice: external slabs (porch, paving, patio, carport ...) are separate from
    the building slab, isolated with an expansion joint. Adds the note where the sheet has
    one and no note already covers it. Returns True when it added the note."""
    texts = [s.text for s in sheet.schedule if _prefix(s.tag) == "S"] + \
        [e.text for e in sheet.elements if e.kind == "member" and _prefix(e.tag) == "S"]
    if not any(EXTERNAL_SLAB.search(t or "") for t in texts):
        return False
    said = " ".join(sheet.notes + [e.text for e in sheet.elements if e.kind == "callout"]).upper()
    if "ABELFLEX" in said or "EXPANSION JOINT" in said:
        return False
    sheet.notes.append("-EXTERNAL SLABS (PORCH / PAVING / PATIO / CARPORT) ARE SEPARATE FROM THE BUILDING SLAB: "
                       "ISOLATE FROM BUILDING SLAB / FOOTING AND FROM POSTS / PIERS WITH 10 ABELFLEX EXPANSION "
                       "JOINT AND MASTIC SEAL. PAVING SLABS SAW CUT OR TOOLED JOINTS AT MAX 1500 CTRS.")
    return True


def _draw_element(page: pymupdf.Page, e: Element, m: PlanMap) -> None:
    a, b = m(e.start), m(e.end)
    colour = tag_colour(e.tag, e.kind)
    content = e.text or e.tag
    if e.kind == "member" and id(e) in m.bands:
        for p, q, dashed in m.bands[id(e)]:
            _line(page, p, q, colour, 1.0 if dashed else 1.25, f"Member {_prefix(e.tag)}", content,
                  dashes=[2, 2] if dashed else None)
            m.segments.append((p, q))
        if e.tag:
            m.label_requests.append(("line", (a, b), e.tag, TAG_FS, colour, "Member Tag"))
    elif e.kind == "member" and _prefix(e.tag) == "S" and abs(b - a) > 20:
        _slab_line(page, e, a, b, colour, m)
    elif e.kind == "member":
        # Slab diagonal (S): a fine line, as the engineers draw it.
        width = 1.25 if _prefix(e.tag) == "S" else 3.25 if _prefix(e.tag) in FOOTING_PREFIXES else 2.75
        _line(page, a, b, colour, width, f"Member {_prefix(e.tag) or ''}".strip(), content)
        m.segments.append((a, b))
        if e.tag:
            m.label_requests.append(("line", (a, b), e.tag, TAG_FS, colour, "Member Tag"))
    elif e.kind == "span":
        _arrow(page, a, b, colour, 1.0, "Span", content)
        m.segments.append((a, b))
        if e.tag:
            m.label_requests.append(("line", (a, b), e.tag, TAG_FS, colour, "Member Tag"))
    elif e.kind == "bracing":
        dashed = e.tag.strip().lower().startswith("d")
        _line(page, a, b, TEAL, 1.5, "Bracing", content, dashes=[3, 3] if dashed else None)
        m.segments.append((a, b))
        if e.tag:
            m.label_requests.append(("line", (a, b), e.tag, TAG_FS, TEAL, "Bracing Tag"))
    elif e.kind == "pier":
        dia, side = _sizes(f"{e.text} {m.spec(e.tag)}")
        r = _scaled(m, (dia or PIER_DEFAULT_MM) / 2, 6.0, 2.5, 16.0)
        _circle = page.add_circle_annot(pymupdf.Rect(a.x - r, a.y - r, a.x + r, a.y + r))
        _circle.set_border(width=1, dashes=[2, 2])
        _circle.set_colors(stroke=RED)
        _info(_circle, "Pier", content)
        _circle.update()
        if side and m.mm_pt:  # brick / block pier on the bored pier, drawn to its size
            h = _scaled(m, side / 2, 2.0, 1.2, 12.0)
            _rect(page, pymupdf.Rect(a.x - h, a.y - h, a.x + h, a.y + h), RED, 0.75, "Pier")
        m.boxes.append(pymupdf.Rect(a.x - r, a.y - r, a.x + r, a.y + r))
        if e.tag:
            m.label_requests.append(("point", (a, r), e.tag, TAG_FS, RED, "Pier Tag"))
    elif e.kind == "column":
        _, side = _sizes(f"{e.text} {m.spec(e.tag)}")
        h = _scaled(m, (side or 90.0) / 2, 2.5, 1.5, 8.0)
        _rect(page, pymupdf.Rect(a.x - h, a.y - h, a.x + h, a.y + h), colour, 0.5, "Column", fill=colour)
        m.boxes.append(pymupdf.Rect(a.x - h, a.y - h, a.x + h, a.y + h))
        if e.tag:
            m.label_requests.append(("point", (a, h), e.tag, TAG_FS, colour, "Column Tag"))
    elif e.kind == "cloud":
        r = pymupdf.Rect(a, b).normalize()
        if r.width < 8 or r.height < 8:
            r = pymupdf.Rect(a.x - 12, a.y - 12, a.x + 12, a.y + 12)
        _rect(page, r, RED, 1.5, "Cloud", clouds=2)
        # The cloud's scallops sit on its edge: keep tags clear of the edge, not the inside.
        for p, q in ((r.tl, r.tr), (r.tr, r.br), (r.br, r.bl), (r.bl, r.tl)):
            m.segments.append((p, q))
        if e.tag or e.text:
            m.label_requests.append(("box", r, e.text or e.tag, TAG_FS, RED, "Cloud Tag"))
    elif e.kind == "tag":
        m.label_requests.append(("point", (a, 0.0), e.text or e.tag, TAG_FS, colour, "Tag"))


# ---------------------------------------------------------------------------
# Callouts: boxed notes in the margins with arrow leaders, like the examples
# ---------------------------------------------------------------------------

CALLOUT_FS = 6.5
CALLOUT_W = 150.0
CALLOUT_MIN_W = 80.0


def _callout_attach(a: pymupdf.Point, box: pymupdf.Rect, horizontal: bool) -> pymupdf.Point:
    """Where a leader leaves its box: the face towards the plan, nearest the anchor."""
    if horizontal:
        return pymupdf.Point(min(max(a.x, box.x0 + 4), box.x1 - 4), box.y1 if a.y > box.y1 else box.y0)
    return pymupdf.Point(box.x0 if a.x < box.x0 else box.x1, min(max(a.y, box.y0 + 3), box.y1 - 3))


def _legs_cross(p1, p2, p3, p4) -> bool:
    def side(a, b, c):
        return (c.x - a.x) * (b.y - a.y) - (c.y - a.y) * (b.x - a.x)
    return side(p3, p4, p1) * side(p3, p4, p2) < 0 and side(p1, p2, p3) * side(p1, p2, p4) < 0


def _callout_line(items: list, rect: pymupdf.Rect, horizontal: bool) -> list | None:
    """Callouts in ONE row (top / bottom margin) or ONE column (right margin), each box as
    near its anchor as the line allows and in the order of the anchors, so no leader runs
    through another box; neighbours whose leaders still cross (a deep anchor beside a
    shallow one) swap places until none do. Returns [(item, box, lines)] or None if they
    don't fit."""
    order = sorted(items, key=lambda t: t[0].x if horizontal else t[0].y)
    row = None
    for _ in range(len(order) ** 2 + 1):
        row = _callout_line_once(order, rect, horizontal)
        if not row:
            return row
        legs = [(it[0], _callout_attach(it[0], box, horizontal)) for it, box, _ in row]
        swap = next((i for i in range(len(row) - 1) if _legs_cross(*legs[i], *legs[i + 1])), None)
        if swap is None:
            return row
        order = [it for it, _, _ in row]
        order[swap], order[swap + 1] = order[swap + 1], order[swap]
    return row


def _callout_line_once(items: list, rect: pymupdf.Rect, horizontal: bool) -> list | None:
    if not items:
        return []
    sized = []
    if horizontal:  # boxes go narrower (and taller) to fit the row
        room = rect.width - 6 * (len(items) - 1)
        widths = [t[3] for t in items]
        cap = max(widths)
        while cap > CALLOUT_MIN_W and sum(min(w, cap) for w in widths) > room:
            cap -= 2
        if sum(min(w, cap) for w in widths) > room:
            return None
        for (a, text, lines, w, h) in items:
            if w > cap:
                lines = _wrap(text, cap - 8, CALLOUT_FS)
                w = min(cap, max(_text_width(ln, CALLOUT_FS) for ln in lines) + 10)
                h = _text_height(len(lines), CALLOUT_FS)
            if h > rect.height:
                return None
            sized.append((a, lines, w, h))
    else:
        sized = [(a, lines, min(w, rect.width), h) for a, text, lines, w, h in items]
        if sum(t[3] for t in sized) + 5 * (len(sized) - 1) > rect.height:
            return None
    lo_edge, hi_edge, gap = (rect.x0, rect.x1, 6) if horizontal else (rect.y0, rect.y1, 5)
    size = [t[2] if horizontal else t[3] for t in sized]
    pos = [(t[0].x if horizontal else t[0].y) - sz / 2 for t, sz in zip(sized, size)]
    for i in range(len(pos)):  # forward: no overlaps, inside the band
        pos[i] = max(pos[i], lo_edge if i == 0 else pos[i - 1] + size[i - 1] + gap)
    for i in range(len(pos) - 1, -1, -1):  # backward
        pos[i] = min(pos[i], (hi_edge if i == len(pos) - 1 else pos[i + 1] - gap) - size[i])
    if pos[0] < lo_edge - 0.5:
        return None
    out = []
    for item, (a, lines, w, h), p in zip(items, sized, pos):
        if horizontal:  # on the face of the band nearest the plan
            y = rect.y1 - h if rect.y1 < a.y else rect.y0
            box = pymupdf.Rect(p, y, p + w, y + h)
        else:
            box = pymupdf.Rect(rect.x0, p, rect.x0 + w, p + h)
        out.append((item, box, lines))
    return out


def _draw_callouts(page: pymupdf.Page, callouts: list[Element], m: PlanMap, bottom_band=BAND_BOTTOM) -> list[str]:
    """Boxed notes in the margins with arrow leaders: one row along the top, one column down
    the right and one row along the bottom, each in the order of its anchors, so callouts
    and their leaders don't overlap one another. What doesn't fit goes to the notes."""
    centre_y = (m.placed.y0 + m.placed.y1) / 2
    right_x = m.placed.x1 - m.placed.width * 0.25
    groups: dict[int, list] = {0: [], 1: [], 2: []}
    for e in callouts:
        a = m(e.start)
        text = (e.text or e.tag).upper()
        lines = _wrap(text, CALLOUT_W - 8, CALLOUT_FS)
        h = _text_height(len(lines), CALLOUT_FS)
        w = min(CALLOUT_W, max(_text_width(ln, CALLOUT_FS) for ln in lines) + 10)
        side = 1 if a.x > right_x else (0 if a.y < centre_y else 2)
        groups[side].append((a, text, lines, w, h))
    bands = {0: (BAND_TOP, True), 1: (BAND_RIGHT, False), 2: (bottom_band, True)}

    def fits(b, items):
        return _callout_line(items, *bands[b]) is not None

    unplaced: list[str] = []
    for b in (0, 2, 1):
        while not fits(b, groups[b]):
            # Give up the callout nearest another band: the right-most for a row, the one
            # nearest the top / bottom for the column.
            if b == 1:
                out = max(groups[1], key=lambda t: abs(t[0].y - centre_y))
                others = [0, 2] if out[0].y < centre_y else [2, 0]
            else:
                out = max(groups[b], key=lambda t: t[0].x)
                others = [1, 2 - b]
            groups[b].remove(out)
            dest = next((o for o in others if o != b and fits(o, groups[o] + [out])), None)
            if dest is None:
                unplaced.append(out[1])
            else:
                groups[dest].append(out)

    def layout():
        return {b: _callout_line(groups[b], *bands[b]) for b in (0, 1, 2)}

    def crossings(lay):
        legs = [(it[0], _callout_attach(it[0], box, bands[b][1]), id(it)) for b in lay for it, box, _ in lay[b]]
        return [(x[2], y[2]) for i, x in enumerate(legs) for y in legs[i + 1:] if _legs_cross(x[0], x[1], y[0], y[1])]

    # Leaders from different bands that cross: move one of the pair into the other's band
    # where that leaves fewer crossings.
    lay = layout()
    for _ in range(3 * len(callouts)):
        bad = crossings(lay)
        if not bad:
            break
        best = None
        for ida, idb in bad:
            ba = next(b for b in groups if any(id(t) == ida for t in groups[b]))
            bb = next(b for b in groups if any(id(t) == idb for t in groups[b]))
            if ba == bb:
                continue
            for it_id, src, dst in ((ida, ba, bb), (idb, bb, ba)):
                it = next(t for t in groups[src] if id(t) == it_id)
                if not fits(dst, groups[dst] + [it]):
                    continue
                groups[src].remove(it)
                groups[dst].append(it)
                n = len(crossings(layout()))
                groups[dst].remove(it)
                groups[src].append(it)
                if n < len(bad) and (best is None or n < best[0]):
                    best = (n, it, src, dst)
        if best is None:
            break
        _, it, src, dst = best
        groups[src].remove(it)
        groups[dst].append(it)
        lay = layout()
    for b in (0, 1, 2):
        horizontal = bands[b][1]
        for item, box, lines in lay[b]:
            a = item[0]
            attach = _callout_attach(a, box, horizontal)
            _text(page, box, "\n".join(lines), CALLOUT_FS, RED, "Call-out", align=1, border=0.5,
                  fill=(1, 1, 1), callout=[a, attach])
            m.boxes.append(box)
            m.segments.append((a, attach))
    return unplaced


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------


@dataclass
class SheetMeta:
    project: str
    job_no: str
    rev: str
    made_by: str
    date: str
    title_block: bool = False
    masonry: bool = False  # brick veneer / masonry walls: wall sheet gets the masonry lintel schedule


# Office masonry lintel schedule (office_rules.md): steel lintels for the outer brick skin.
MASONRY_LINTELS = [("UP TO 900", "75x10 FL"), ("900 - 1500", "100x75x8 UA"), ("1500 - 3300", "150x100x10 UA")]
MASONRY_LINTEL_NOTE = "ALL MASONRY LINTELS TO BEAR MIN 150 INTO BRICKWORK EACH END. OVER 3300 BY ENGINEER."


def _is_masonry(design) -> bool:
    if design is None:
        return False
    text = " ".join(str(getattr(design, k, "") or "") for k in ("external_wall_type", "framing_material", "governing_standard"))
    return re.search(r"brick|masonry|block|veneer|AS ?3700|AS ?4773", text, re.I) is not None


def _new_sheet(out: pymupdf.Document, meta: SheetMeta, title: str, dwg_no: str) -> pymupdf.Page:
    page = out.new_page(width=A3.width, height=A3.height)
    _title(page, meta.project, title)
    if meta.title_block:
        _title_block(page, meta.project, title, dwg_no, meta.job_no, meta.rev, meta.made_by, meta.date)
    return page


def _is_lintel(e: Element) -> bool:
    return e.kind == "member" and _prefix(e.tag) == "L"


def _lintel_from_codes(p: pymupdf.Point, q: pymupdf.Point, coded) -> tuple[pymupdf.Point, pymupdf.Point] | None:
    """Lintel snap: onto the architect's coded opening (A1812 ...) it sits over - same
    wall direction, within reach across, overlapping it or within about an opening
    width along (the nearest wins). Bearing ~10% of the span (max 3 pt) each side."""
    horiz = abs(q.x - p.x) >= abs(q.y - p.y)
    lo, hi = sorted((p.x, q.x) if horiz else (p.y, q.y))
    c0 = (p.y + q.y) / 2 if horiz else (p.x + q.x) / 2
    best = None
    for o in coded:
        if o.horizontal != horiz or abs(o.c - c0) > 12:
            continue
        width = o.a1 - o.a0
        overlap = min(hi, o.a1) - max(lo, o.a0)
        # Overlapping it, or just off it (Claude's placement can be a window-width out).
        if overlap < 0.3 * width and abs((lo + hi) / 2 - (o.a0 + o.a1) / 2) > width + 6:
            continue
        score = abs((lo + hi) / 2 - (o.a0 + o.a1) / 2) + abs(o.c - c0)
        if best is None or score < best[0]:
            best = (score, o)
    if best is None:
        return None
    o = best[1]
    bear = min(3.0, 0.1 * (o.a1 - o.a0))
    s, e = o.a0 - bear, o.a1 + bear
    return (pymupdf.Point(s, o.c), pymupdf.Point(e, o.c)) if horiz else (pymupdf.Point(o.c, s), pymupdf.Point(o.c, e))


def _snap_to_drawing(src_page: pymupdf.Page, sheet: MarkupSheet, title: str) -> list[str]:
    """Move members onto the real geometry of a vector drawing (see snap.py):
    lintels onto their openings; footings, bearers and beams onto the walls they run
    along; bracing panels onto their walls at their scaled length, off openings;
    joist / rafter ends onto the walls, bearers and beams they bear on; piers and
    columns onto the member or wall they support (a member end / crossing if close).
    Returns warnings naming anything that should have snapped but didn't."""
    lintels = [i for i, e in enumerate(sheet.elements) if _is_lintel(e)]
    walled = [i for i, e in enumerate(sheet.elements)
              if e.kind == "member" and _prefix(e.tag) in FOOTING_TAGS | BEAM_TAGS]
    bracing = [i for i, e in enumerate(sheet.elements) if e.kind == "bracing"]
    spans = [i for i, e in enumerate(sheet.elements)
             if (e.kind in ("span", "member") and _prefix(e.tag) in SPAN_TAGS)
             or (e.kind == "span" and not _prefix(e.tag))]
    points = [i for i, e in enumerate(sheet.elements) if e.kind in ("pier", "column")]
    if not lintels and not walled and not bracing and not spans and not points:
        return []
    try:
        geo = Geometry(src_page)
    except Exception:
        geo = None
    if geo is None or not geo.usable:
        return [f"{title}: drawing has no vector line work (scanned?) - members are placed from "
                "Claude's reading of the image; check lintels, bearers, beams and bracing line up"]
    w, h = src_page.rect.width, src_page.rect.height

    def pts(e: Element) -> tuple[pymupdf.Point, pymupdf.Point]:
        return pymupdf.Point(e.start.x * w, e.start.y * h), pymupdf.Point(e.end.x * w, e.end.y * h)

    def put(i: int, r) -> None:
        sheet.elements[i] = sheet.elements[i].model_copy(update={
            "start": Point(x=r[0].x / w, y=r[0].y / h),
            "end": Point(x=r[1].x / w, y=r[1].y / h),
        })

    warnings: list[str] = []
    missed: list[str] = []
    # The architect's coded openings (A1812 = 1800 high x 1200 wide ...) on this page.
    coded = []
    if lintels:
        scale, _ = drawing_scale(src_page, geo)
        if scale:
            try:
                coded = find_openings(src_page, geo, scale)
            except Exception:
                coded = []
    specs = {s.tag.strip().upper(): s.text.upper() for s in sheet.schedule}
    for i in lintels:
        e = sheet.elements[i]
        if any(w in f"{specs.get(e.tag.strip().upper(), '')} {e.text}".upper() for w in ("CONTINUOUS", "CONTINOUS", "LEDGER")):
            # A continuous lintel / ledger runs over several openings: keep its length and
            # only bring it onto its wall (snapping it to one opening would cut it short).
            r = snap_to_wall(geo, *pts(e))
            if r is not None:
                put(i, r)
            continue
        # The architect's code picks WHICH opening; its jamb lines then give the exact edges.
        rc = _lintel_from_codes(*pts(sheet.elements[i]), coded)
        if rc is not None:
            r = snap_span(geo, *rc) or rc
        else:
            r = snap_span(geo, *pts(sheet.elements[i]))
        if r is None:
            missed.append(sheet.elements[i].tag)
        else:
            put(i, r)
    if missed:
        warnings.append(f"{title}: {len(lintels) - len(missed)} of {len(lintels)} lintels snapped to openings in the "
                        f"drawing; left as placed by Claude, check: {', '.join(missed)}")

    if walled:
        piers_cols = [pts(e)[0] for e in sheet.elements if e.kind in ("pier", "column")]
        results = snap_members(geo, [(_prefix(sheet.elements[i].tag), *pts(sheet.elements[i])) for i in walled],
                               piers_cols)
        off_wall_footings = []
        for i, r in zip(walled, results):
            if r is not None:
                put(i, r)
            elif _prefix(sheet.elements[i].tag) in FOOTING_TAGS:
                off_wall_footings.append(sheet.elements[i].tag)
        if off_wall_footings:
            # Bearers / beams can legitimately span mid-room; footings normally sit under walls.
            warnings.append(f"{title}: footing line(s) not on a wall in the drawing, left as placed by Claude, "
                            f"check: {', '.join(off_wall_footings)}")
    if bracing:
        scale, how = drawing_scale(src_page, geo)
        if scale is None:
            warnings.append(f"{title}: drawing scale not found, so bracing panels keep the lengths Claude "
                            "drew; check them against their tags")
        not_on_wall, problems = [], []
        for i in bracing:
            e = sheet.elements[i]
            mm = bracing_length_mm(e.tag)
            r = snap_bracing(geo, *pts(e), mm_to_pt(mm, scale) if mm and scale else None)
            if r is None:
                not_on_wall.append(e.tag or "bracing")
                continue
            put(i, r[:2])
            if r[2] and not r[2].startswith("moved"):
                problems.append(f"{e.tag} ({r[2]})")
        if not_on_wall:
            warnings.append(f"{title}: bracing not on a wall in the drawing, left as placed by Claude, check: "
                            f"{', '.join(not_on_wall)}")
        if problems:
            warnings.append(f"{title}: bracing that may be over an opening, check: {'; '.join(problems)}")
    if lintels or bracing:
        warnings += _contain(geo, sheet, lintels, bracing, pts, put, title, src_page)
        lintels = [i for i, e in enumerate(sheet.elements) if _is_lintel(e)]
        spans = [i for i, e in enumerate(sheet.elements)
                 if (e.kind in ("span", "member") and _prefix(e.tag) in SPAN_TAGS)
                 or (e.kind == "span" and not _prefix(e.tag))]
        points = [i for i, e in enumerate(sheet.elements) if e.kind in ("pier", "column")]
    _turn_span_arrows(sheet, pts, put)
    joist_fp = None
    if any(_prefix(e.tag) == "J" for e in sheet.elements):
        try:
            joist_fp = Footprint(geo)
        except Exception:
            joist_fp = None
    if spans:
        # Supports: walls in the drawing plus this sheet's (already snapped) bearers,
        # beams, ledgers, lintels and footings.
        supports = []
        for e in sheet.elements:
            if e.kind == "member" and (_prefix(e.tag) in BEAM_TAGS | FOOTING_TAGS or _prefix(e.tag) == "L"):
                a, b = pts(e)
                horiz = abs(b.x - a.x) >= abs(b.y - a.y)
                lo, hi = sorted((a.x, b.x) if horiz else (a.y, b.y))
                supports.append((horiz, (a.y + b.y) / 2 if horiz else (a.x + b.x) / 2, lo, hi))
        for i in spans:
            r = snap_span_ends(geo, *pts(sheet.elements[i]), supports)
            if r is not None and r[2]:
                put(i, r[:2])
    perimeter = []
    for e in sheet.elements:  # perimeter footings (already snapped onto the walls)
        if e.kind == "member" and _prefix(e.tag) in ("SF", "EB"):
            a, b = pts(e)
            h_ = abs(b.x - a.x) >= abs(b.y - a.y)
            lo, hi = sorted((a.x, b.x) if h_ else (a.y, b.y))
            perimeter.append((h_, (a.y + b.y) / 2 if h_ else (a.x + b.x) / 2, lo, hi))
    if perimeter or (joist_fp is not None and joist_fp.ok):
        # Floor joists wall to wall; their arrows across them, ending at the walls.
        for i, e in enumerate(sheet.elements):
            if _prefix(e.tag) == "J" and e.kind in ("member", "span"):
                r = _wall_to_wall(geo, joist_fp if joist_fp is not None and joist_fp.ok else None,
                                  *pts(e), perimeter=perimeter or None)
                if r is not None:
                    put(i, r)
    if points:
        # Lines a pier / column can sit on: this sheet's (snapped) members and the walls.
        members = []
        for e in sheet.elements:
            if e.kind == "member" and _prefix(e.tag) not in SPAN_TAGS:
                a, b = pts(e)
                horiz = abs(b.x - a.x) >= abs(b.y - a.y)
                lo, hi = sorted((a.x, b.x) if horiz else (a.y, b.y))
                members.append((horiz, (a.y + b.y) / 2 if horiz else (a.x + b.x) / 2, lo, hi))
        lines = members + [(True, c, lo, hi) for c, lo, hi, _ in _wall_runs(geo, True)] \
            + [(False, c, lo, hi) for c, lo, hi, _ in _wall_runs(geo, False)]
        targets = point_targets(lines, members)
        for i in points:
            e = sheet.elements[i]
            r = snap_point(pts(e)[0], lines, targets)
            if r is not None:
                put(i, (r, r))
    return warnings


_CTRS = re.compile(r"(\d{3,4})\s*(?:MM\s*)?(?:MAX\.?\s*)?(?:CTRS|CENTRES|CENTERS|CRS|C/C)\b", re.I)


def _spacing_check(sheet: MarkupSheet, page: pymupdf.Page, scale: int | None, title: str) -> list[str]:
    """Bearers / rafters drawn further apart than their scheduled spacing (e.g. 'AT MAX
    2200 CTRS'), measured at the plan scale. Rafters with a spacing but only one line drawn
    are named too."""
    if not scale:
        return []
    w, h = page.rect.width, page.rect.height
    per_mm = mm_to_pt(1, scale)
    spec = {s.tag.strip().upper(): s.text for s in sheet.schedule}
    groups: dict[str, list] = {}
    for e in sheet.elements:
        if e.kind == "member" and _prefix(e.tag) in ("B", "R"):
            groups.setdefault(e.tag.strip().upper(), []).append(e)
    out = []
    for tag, els in groups.items():
        m = _CTRS.search(spec.get(tag, "")) or _CTRS.search(" ".join(e.text for e in els))
        if not m:
            continue
        ctrs = float(m.group(1))
        lines = []
        for e in els:
            p, q = pymupdf.Point(e.start.x * w, e.start.y * h), pymupdf.Point(e.end.x * w, e.end.y * h)
            horiz = abs(q.x - p.x) >= abs(q.y - p.y)
            lo, hi = sorted((p.x, q.x) if horiz else (p.y, q.y))
            lines.append((horiz, (p.y + q.y) / 2 if horiz else (p.x + q.x) / 2, lo, hi))
        what = "bearers" if _prefix(tag) == "B" else "rafters"
        worst = 0.0
        for horiz in (True, False):
            ls = sorted(l for l in lines if l[0] == horiz)
            for a, b in zip(ls, ls[1:]):
                if min(a[3], b[3]) - max(a[2], b[2]) > 0:  # side by side, not end to end
                    worst = max(worst, (b[1] - a[1]) / per_mm)
        if worst > ctrs * 1.25:
            out.append(f"{title}: {tag} {what} are drawn up to {worst:.0f} mm apart but scheduled at "
                       f"{ctrs:.0f} ctrs - check the layout (a {what[:-1]} may be missing)")
    return out


def _wall_to_wall(geo: Geometry, fp: Footprint | None, p: pymupdf.Point, q: pymupdf.Point,
                  perimeter: list | None = None):
    """Stretch / trim a line inside the building to run from wall to wall: along its own
    line, out from its middle to the external walls on each side. The external walls are
    this sheet's perimeter footing lines (SF / EB) when it has them - they follow recesses
    and decks exactly - otherwise the building outline. Each end goes on that wall's
    centreline. Lines whose middle is outside the building (decks) are left alone."""
    horiz = abs(q.x - p.x) >= abs(q.y - p.y)
    c = (p.y + q.y) / 2 if horiz else (p.x + q.x) / 2
    mid = (p.x + q.x) / 2 if horiz else (p.y + q.y) / 2
    at = (lambda v: pymupdf.Point(v, c)) if horiz else (lambda v: pymupdf.Point(c, v))
    if perimeter:
        cross = [pc for ph, pc, lo, hi in perimeter if ph != horiz and lo - 2 <= c <= hi + 2]
        below, above = [v for v in cross if v < mid - 4], [v for v in cross if v > mid + 4]
        if below and above:
            return at(max(below)), at(min(above))
        return None  # outside the footing outline (e.g. a deck): leave it
    if fp is None or not fp.contains(at(mid), tol=0):
        return None
    ends = []
    for step in (-2.0, 2.0):
        v = mid
        while fp.contains(at(v + step), tol=0) and abs(v - mid) < 1500:
            v += step
        ends.append(v)
    walls = [w for w in _wall_runs(geo, not horiz) if w[1] - w[3] <= c <= w[2] + w[3]]

    def snap(v: float, inward: float) -> float:
        near = [w[0] for w in walls if abs(w[0] - v) <= max(8.0, w[3] * 1.5)]
        return min(near, key=lambda x: abs(x - (v + inward))) if near else v

    a, b = snap(ends[0], 2.0), snap(ends[1], -2.0)
    if b - a < 8:
        return None
    return at(a), at(b)


def _turn_span_arrows(sheet: MarkupSheet, pts, put) -> None:
    """House style: a joist / rafter arrow runs PERPENDICULAR to the members' span (across
    them, showing how far they repeat), and a representative 'member' runs along the span.
    Each arrow is checked against the NEAREST member with its tag (the same tag can be used
    in areas spanning different ways, e.g. two decks), and turned 90 degrees about its
    middle if it was drawn along that member."""
    members: dict[str, list] = {}
    for e in sheet.elements:
        if e.kind == "member" and _prefix(e.tag) in SPAN_TAGS:
            members.setdefault(e.tag.strip().upper(), []).append(pts(e))

    def dist(pt: pymupdf.Point, a: pymupdf.Point, b: pymupdf.Point) -> float:
        d = b - a
        n2 = d.x * d.x + d.y * d.y
        t = 0.0 if n2 == 0 else max(0.0, min(1.0, ((pt - a).x * d.x + (pt - a).y * d.y) / n2))
        return abs(pt - (a + d * t))

    for i, e in enumerate(sheet.elements):
        if e.kind != "span" or _prefix(e.tag) not in ("J", "R"):
            continue  # truss arrows (T) show the truss span direction, so they stay along it
        mine = members.get(e.tag.strip().upper())
        if not mine:
            continue  # no representative member to go by
        a, b = pts(e)
        mid = (a + b) * 0.5
        ma, mb = min(mine, key=lambda m: dist(mid, *m))
        member_horiz = abs(mb.x - ma.x) >= abs(mb.y - ma.y)
        if (abs(b.x - a.x) >= abs(b.y - a.y)) != member_horiz:
            continue  # already across the members
        half = abs(b - a) / 2
        put(i, (pymupdf.Point(mid.x, mid.y - half), pymupdf.Point(mid.x, mid.y + half)) if member_horiz
            else (pymupdf.Point(mid.x - half, mid.y), pymupdf.Point(mid.x + half, mid.y)))

    # Every representative joist / rafter has its own arrow across it: pair each arrow with
    # its nearest member, then add an arrow (through the member's middle, across it) for any
    # member left without one. Floor-joist arrows are then run wall to wall.
    arrows = [(e.tag.strip().upper(), pts(e)) for e in sheet.elements
              if e.kind == "span" and _prefix(e.tag) in ("J", "R")]
    owned: set[tuple] = set()
    for tag, (a, b) in arrows:
        mine = members.get(tag)
        if mine:
            mid = (a + b) * 0.5
            m = min(mine, key=lambda m: dist(mid, *m))
            owned.add((tag, round(m[0].x, 1), round(m[0].y, 1)))
    for e in list(sheet.elements):
        if e.kind != "member" or _prefix(e.tag) not in ("J", "R"):
            continue
        tag = e.tag.strip().upper()
        ma, mb = pts(e)
        if (tag, round(ma.x, 1), round(ma.y, 1)) in owned:
            continue
        mid, half = (ma + mb) * 0.5, max(abs(mb - ma), 40.0) / 2
        horiz = abs(mb.x - ma.x) >= abs(mb.y - ma.y)
        sheet.elements.append(e.model_copy(update={"kind": "span", "text": ""}))
        put(len(sheet.elements) - 1,
            (pymupdf.Point(mid.x, mid.y - half), pymupdf.Point(mid.x, mid.y + half)) if horiz
            else (pymupdf.Point(mid.x - half, mid.y), pymupdf.Point(mid.x + half, mid.y)))
        owned.add((tag, round(ma.x, 1), round(ma.y, 1)))

def _contain(geo: Geometry, sheet: MarkupSheet, lintels: list[int], bracing: list[int], pts, put,
             title: str, src_page: pymupdf.Page) -> list[str]:
    """Firm rule: lintels and bracing are inside the building (its walls and the rooms
    they enclose), never on a deck, verandah, carport or eaves line. One outside is
    moved onto the nearest building wall alongside it (then re-snapped to its opening
    / wall there); one with no building wall nearby is removed. Returns warnings."""
    fp = Footprint(geo)
    if not fp.ok:
        return [f"{title}: building outline not found in the drawing - check every lintel and bracing "
                "panel is on a building wall (not a deck, verandah, carport or eaves)"]
    scale = None
    moved, removed, drop = [], [], set()
    for i in lintels + bracing:
        e = sheet.elements[i]
        p, q = pts(e)
        if fp.line_inside(p, q, tol=4):
            continue
        name = e.tag or ("bracing" if e.kind == "bracing" else "lintel")
        r = fp.move_inside(p, q)
        if r is None:
            drop.add(i)
            removed.append(name)
            continue
        if e.kind == "bracing":
            if scale is None:
                scale, _ = drawing_scale(src_page, geo)
            mm = bracing_length_mm(e.tag)
            s = snap_bracing(geo, *r, mm_to_pt(mm, scale) if mm and scale else None)
        else:
            s = snap_span(geo, *r)
        put(i, s[:2] if s is not None and fp.line_inside(*s[:2], tol=4) else r)
        moved.append(name)
    if drop:
        sheet.elements = [e for k, e in enumerate(sheet.elements) if k not in drop]
    out = []
    if moved:
        out.append(f"{title}: lintel / bracing outside the building (deck, verandah, carport or eaves) "
                   f"moved onto the nearest building wall, check: {', '.join(moved)}")
    if removed:
        out.append(f"{title}: lintel / bracing outside the building with no building wall nearby removed "
                   f"(lintels and bracing must be within the building), re-add if needed: {', '.join(removed)}")
    return out


def _articulation(src_page: pymupdf.Page, sheet: MarkupSheet, plan_page: pymupdf.Page | None,
                  region: pymupdf.Rect, title: str) -> list[str]:
    """Articulation sheet (on the elevation page): joints vertical and out of every
    window / door seen in the elevations; cross-check the elevation openings with the
    plan's coded openings."""
    geo = Geometry(src_page)
    if not geo.usable:
        return [f"{title}: elevation has no vector line work (scanned?) - check no joint passes through "
                "a window or door"]
    scale, _ = drawing_scale(src_page, geo)
    openings = [r for r in find_elevation_openings(geo, scale) if region.contains(r)]
    w, h = src_page.rect.width, src_page.rect.height
    moved = []
    for i, e in enumerate(sheet.elements):
        if e.kind != "member" or _prefix(e.tag) not in ("AJ", "CJ"):
            continue
        p = pymupdf.Point(e.start.x * w, e.start.y * h)
        q = pymupdf.Point(e.end.x * w, e.end.y * h)
        a, b, note = snap_joint_elevation(p, q, openings)
        sheet.elements[i] = e.model_copy(update={"start": Point(x=a.x / w, y=a.y / h), "end": Point(x=b.x / w, y=b.y / h)})
        if note:
            moved.append(e.tag)
    warnings = []
    if moved:
        warnings.append(f"{title}: joint(s) ran through a window / door on the elevation and were moved to its "
                        f"jamb, check: {', '.join(moved)}")
    if plan_page is not None and scale:
        try:
            pgeo = Geometry(plan_page)
            pscale, _ = drawing_scale(plan_page, pgeo)
            coded = find_openings(plan_page, pgeo, pscale) if pscale else []
            if coded and openings:
                for msg in cross_check([o.width_mm for o in coded], openings, scale):
                    warnings.append(f"{title}: plan vs elevation line work - {msg} (check; doors without "
                                    "codes, balustrades and openings the drawing reader missed also show here)")
        except Exception:
            pass
    return warnings


def _markup_sheet(out, src, meta: SheetMeta, kind: SheetKind, sheet: MarkupSheet, dwg_no: str,
                  plan_idx: int | None = None) -> tuple[list[str], list[str]]:
    """Draw one sheet. Returns (notes that did not fit, warnings)."""
    warnings: list[str] = []
    page = _new_sheet(out, meta, SHEET_TITLES[kind], dwg_no)
    idx = min(max(sheet.source_page_number, 1), src.page_count) - 1
    grown = _fit_region(sheet)
    if grown:
        warnings.append(f"{dwg_no}: {grown} mark-up(s) were placed outside the plan area Claude chose, so the "
                        "plan area was enlarged to show them - check they are on the right drawing")
    m = PlanMap(src[idx], sheet.plan_region)
    try:  # draw piers / columns roughly to scale
        scale, _ = drawing_scale(src[idx], Geometry(src[idx]))
        if scale:
            m.mm_pt = mm_to_pt(1, scale) * m.scale
    except Exception:
        pass
    m.specs = {s.tag.strip().upper(): s.text for s in sheet.schedule}
    if kind in (SheetKind.FOUNDATION, SheetKind.FOOTINGS, SheetKind.FLOOR):
        try:
            warnings += _footing_house_style(sheet, src[idx], SHEET_TITLES[kind])
        except Exception as e:
            warnings.append(f"{dwg_no}: footing tags not checked ({e})")
    if kind == SheetKind.ARTICULATION:
        try:
            plan_page = src[plan_idx] if plan_idx is not None and 0 <= plan_idx < src.page_count else None
            warnings += _articulation(src[idx], sheet, plan_page, m.clip, SHEET_TITLES[kind])
        except Exception as e:
            warnings.append(f"{dwg_no}: articulation joint check skipped ({e})")
    else:
        try:
            warnings += _snap_to_drawing(src[idx], sheet, SHEET_TITLES[kind])
        except Exception as e:
            warnings.append(f"{dwg_no}: snapping to the drawing skipped ({e})")
    try:
        scale_ = drawing_scale(src[idx])[0]
        warnings += _spacing_check(sheet, src[idx], scale_, SHEET_TITLES[kind])
        warnings += office_checks.pier_spacing(sheet, src[idx], scale_, SHEET_TITLES[kind])
        warnings += office_checks.schedule_practice(sheet, SHEET_TITLES[kind])
        warnings += office_checks.footing_practice(sheet, SHEET_TITLES[kind])
        warnings += office_checks.external_slabs(sheet, SHEET_TITLES[kind])
    except Exception:
        pass
    if kind == SheetKind.WALLS:
        try:
            geo = Geometry(src[idx])
            scale, _ = drawing_scale(src[idx], geo)
            warnings += check_lintels(src[idx], geo, scale, sheet, SHEET_TITLES[kind])
        except Exception as e:
            warnings.append(f"{dwg_no}: lintel check skipped ({e})")
    try:
        m.draw_background(page, src)
    except Exception as e:
        warnings.append(f"{dwg_no}: architectural background not shown ({e})")
    try:
        m.bands = _footing_bands(sheet, m)
    except Exception as e:  # fall back to single footing lines
        warnings.append(f"{dwg_no}: footings drawn as single lines ({e})")
    callouts: list[Element] = []
    off_plan: list[str] = []
    skipped = 0
    slab_tags = {e.tag.strip().upper() for e in sheet.elements if e.kind == "member" and _prefix(e.tag) == "S"}
    for e in sheet.elements:
        on_plan = m.inside(e.start) and m.inside(e.end)
        if e.kind == "callout" and any(re.match(rf"\s*{re.escape(t)}\b", e.text or "", re.I) for t in slab_tags):
            continue  # the slab callout sits on the slab line itself
        if e.kind == "callout":
            if on_plan:
                callouts.append(e)
            else:
                off_plan.append(e.text or e.tag)
        elif on_plan:
            try:
                _draw_element(page, e, m)
            except Exception:
                skipped += 1
    if skipped:
        warnings.append(f"{dwg_no}: {skipped} plan element(s) could not be drawn")
    if kind in (SheetKind.FOUNDATION, SheetKind.FOOTINGS, SheetKind.FLOOR):
        try:
            _reentrant_bars(page, sheet, m)
        except Exception as e:
            warnings.append(f"{dwg_no}: re-entrant bars not drawn ({e})")
        _external_slab_note(sheet)
    # Callouts first, so the tags keep clear of their boxes and leaders.
    try:
        unplaced = _draw_callouts(page, callouts, m, BAND_BOTTOM if meta.title_block else BAND_BOTTOM_NO_TB)
    except Exception as e:
        warnings.append(f"{dwg_no}: callouts moved to notes ({e})")
        unplaced = [(c.text or c.tag).upper() for c in callouts]
    try:
        _place_labels(page, m)
    except Exception as e:
        warnings.append(f"{dwg_no}: tags not placed ({e})")
    dropped = sum(1 for e in sheet.elements if e.kind != "callout" and not (m.inside(e.start) and m.inside(e.end)))
    if dropped:
        warnings.append(f"{dwg_no}: {dropped} mark-up(s) fell outside the plan and were not drawn")
    if not any(e.kind != "callout" for e in sheet.elements):
        warnings.append(f"{dwg_no}: Claude returned no members / piers / bracing for this sheet - re-run it")
    unplaced += [t.upper() for t in off_plan if t]
    notes = list(sheet.notes) + [f"-{t}" for t in unplaced]  # never drop a note
    overflow = _notes(page, sheet.classification, notes)
    try:
        _schedule(page, sheet.schedule_title, sheet.schedule, sheet.legend_title, sheet.legend,
                  SCHEDULE if meta.title_block else SCHEDULE_NO_TB,
                  masonry=meta.masonry and kind == SheetKind.WALLS)
    except Exception as e:
        warnings.append(f"{dwg_no}: schedule not drawn ({e})")
    return [f"{dwg_no}: {n.lstrip('-')}" for n in overflow], warnings


NOTES_COLS = [pymupdf.Rect(15, 95, 395, 832), pymupdf.Rect(405, 95, 785, 832), pymupdf.Rect(795, 95, 1180, 832)]
NOTES_COLS_TB = [pymupdf.Rect(15, 95, 395, 832), pymupdf.Rect(405, 95, 785, 676), pymupdf.Rect(795, 95, 1180, 676)]
CONT_FS = 7.5


def _notes_continuation(out, meta: SheetMeta, notes: list[str]) -> None:
    """Only used when a sheet's notes do not fit its notes column - nothing is dropped."""
    cols = NOTES_COLS_TB if meta.title_block else NOTES_COLS
    width = min(c.width for c in cols) - 8
    lines = _wrap("NOTES:\n\n" + "\n\n".join(f"-{n}" for n in notes), width, CONT_FS)
    per_line = CONT_FS * 1.18
    n_page = 0
    while lines:
        n_page += 1
        page = _new_sheet(out, meta, "SHEET NOTES (CONT.)", f"SSK-N{n_page}")
        for col in cols:
            if not lines:
                break
            n = max(1, int((col.height - 6) / per_line))
            chunk, lines = lines[:n], lines[n:]
            _text(page, pymupdf.Rect(col.x0, col.y0, col.x1, col.y0 + _text_height(len(chunk), CONT_FS)),
                  "\n".join(chunk), CONT_FS, BLACK, "Notes")


CALC_FS = 7.0


def _calc_sheet(out, meta: SheetMeta, lines: list[str]) -> None:
    """The calculation sheet: text flowed down three columns, continued on further pages."""
    cols = NOTES_COLS_TB if meta.title_block else NOTES_COLS
    width = min(c.width for c in cols) - 8
    wrapped: list[str] = []
    for ln in lines:
        wrapped += _wrap(ln, width, CALC_FS) if ln else [""]
    per_line = CALC_FS * 1.25  # a little more than the rendered line height, so nothing is clipped
    n_page = 0
    while wrapped:
        n_page += 1
        page = _new_sheet(out, meta, "CALCULATION SHEET", f"SSK-C{n_page}")
        for col in cols:
            if not wrapped:
                break
            n = max(1, int((col.height - 6) / per_line))
            chunk, wrapped = wrapped[:n], wrapped[n:]
            while chunk and not chunk[0]:
                chunk = chunk[1:]
            if chunk:
                bottom = min(col.y1, col.y0 + len(chunk) * per_line + 8)
                _text(page, pymupdf.Rect(col.x0, col.y0, col.x1, bottom), "\n".join(chunk), CALC_FS, BLACK,
                      "Calculations")


def build_markup_pdf(
    arch_pdf: bytes,
    project: str,
    geotech: GeotechReview,
    design: DesignAssessment,
    sheets: dict[SheetKind, MarkupSheet],
    references: list[str] = [],
    job_no: str = "",
    rev: str = "A",
    made_by: str = "",
    title_block: bool = False,
    calc_sheet: bool = True,
) -> tuple[bytes, list[str]]:
    """Mark-up sheets (the review summary stays in the app / review.json), plus the
    calculation sheet. Returns (pdf bytes, warnings). Never fails because of one bad element."""
    src = pymupdf.open(stream=arch_pdf, filetype="pdf")
    out = pymupdf.open()
    meta = SheetMeta(project, job_no, rev, made_by, dt.date.today().strftime("%d/%m/%Y"), title_block,
                     masonry=_is_masonry(design))
    overflow: list[str] = []
    warnings: list[str] = []
    n = 1
    for kind in SheetKind:
        if kind not in sheets:
            continue
        dwg_no = f"SSK-{n}"
        before = out.page_count
        try:
            plan_sheet = sheets.get(SheetKind.WALLS) or sheets.get(SheetKind.FOUNDATION) or sheets.get(SheetKind.FLOOR)
            plan_idx = (plan_sheet.source_page_number - 1) if plan_sheet else None
            o, w = _markup_sheet(out, src, meta, kind, sheets[kind], dwg_no, plan_idx)
            overflow += o
            warnings += w
        except Exception as e:  # keep the rest of the set
            while out.page_count > before:
                out.delete_page(-1)
            warnings.append(f"{SHEET_TITLES[kind]} not produced: {e}")
            continue
        n += 1
    if overflow:
        _notes_continuation(out, meta, overflow)
    if calc_sheet:
        try:
            walls = sheets.get(SheetKind.WALLS)
            aspect = 1.414
            if walls and 0 < walls.source_page_number <= src.page_count:
                r = src[walls.source_page_number - 1].rect
                aspect = r.width / r.height
            checks = calcs.bracing_check(design, walls, aspect)
            if walls:
                warnings += calcs.warnings(checks)
            _calc_sheet(out, meta, calcs.lines(geotech, design, checks if walls else [], references))
        except Exception as e:
            warnings.append(f"Calculation sheet not produced: {e}")
    return out.tobytes(garbage=3, deflate=True), warnings
