"""PDF helpers: render architectural pages for Claude and build the mark-up set."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass

import pymupdf

from .schemas import (
    SHEET_TITLES,
    DesignAssessment,
    Element,
    GeotechReview,
    MarkupSheet,
    ScheduleItem,
    SheetKind,
)

MAX_PAGES = 20
MAX_EDGE_PX = 1568  # long edge sent to Claude; keeps detail without oversizing

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
SCHEDULE = pymupdf.Rect(915, 460, 1180, 676)
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
) -> pymupdf.Annot:
    kw = {}
    if callout:
        kw.update(callout=callout, line_end=pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
    a = page.add_freetext_annot(
        rect, text, fontsize=fs, fontname="Helv", text_color=colour, fill_color=fill,
        border_width=border, align=align, **kw,
    )
    _info(a, subject, text)
    a.update()
    return a


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


def _notes(page, classification: str, notes: list[str]) -> None:
    y = NOTES.y0
    if classification:
        cls = classification.upper()
        lines = _wrap(cls, NOTES.width - 8, 10)
        h = _text_height(len(lines), 10)
        _text(page, pymupdf.Rect(NOTES.x0, y, NOTES.x1, y + h), "\n".join(lines), 10, BLACK, "Classification")
        y += h + 8
    if not notes:
        return
    body = "NOTES:\n\n" + "\n\n".join(n if n.lstrip().startswith("-") else f"-{n}" for n in notes)
    for fs in (8.5, 8, 7.5, 7, 6.5, 6, 5.5, 5):
        lines = _wrap(body, NOTES.width - 8, fs)
        h = _text_height(len(lines), fs)
        if y + h <= NOTES.y1:
            break
    _text(page, pymupdf.Rect(NOTES.x0, y, NOTES.x1, min(y + h, NOTES.y1)), "\n".join(lines), fs, BLACK, "Notes")


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


def _schedule(page, title: str, items: list[ScheduleItem], legend_title: str, legend: list[ScheduleItem]) -> None:
    box = SCHEDULE
    entries: list[tuple[str, tuple, bool, str]] = []  # text, colour, is_heading, legend tag
    if items:
        entries.append((title or "SCHEDULE:", BLACK, True, ""))
        entries += [(f"{i.tag}: {i.text}", tag_colour(i.tag), False, "") for i in items]
    if legend:
        if entries:
            entries.append(("", BLACK, False, ""))
        entries.append((legend_title or "LEGEND:", BLACK, True, ""))
        entries += [(i.text, TEAL, False, i.tag) for i in legend]
    if not entries:
        return
    _rect(page, box, BLACK, 1, "Schedule")
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


def _offset_label_point(a: pymupdf.Point, b: pymupdf.Point, dist: float = 4) -> pymupdf.Point:
    mid = pymupdf.Point((a.x + b.x) / 2, (a.y + b.y) / 2)
    horizontal = abs(b.x - a.x) >= abs(b.y - a.y)
    return pymupdf.Point(mid.x - 8, mid.y - dist) if horizontal else pymupdf.Point(mid.x + dist, mid.y + 6)


def _draw_element(page: pymupdf.Page, e: Element, m: PlanMap) -> None:
    a, b = m(e.start), m(e.end)
    colour = tag_colour(e.tag, e.kind)
    content = e.text or e.tag
    if e.kind == "member":
        width = 3.25 if _prefix(e.tag) in FOOTING_PREFIXES else 2.75
        _line(page, a, b, colour, width, f"Member {_prefix(e.tag) or ''}".strip(), content)
        if e.tag:
            _label(page, _offset_label_point(a, b), e.tag, 11, colour, "Member Tag")
    elif e.kind == "span":
        _line(page, a, b, colour, 1.5, "Span", content, arrows=True)
        if e.tag:
            _label(page, _offset_label_point(a, b, 6), e.tag, 11, colour, "Member Tag")
    elif e.kind == "bracing":
        dashed = e.tag.strip().lower().startswith("d")
        _line(page, a, b, TEAL, 1.5, "Bracing", content, dashes=[3, 3] if dashed else None)
        if e.tag:
            _label(page, _offset_label_point(a, b, 3), e.tag, 7, TEAL, "Bracing Tag")
    elif e.kind == "pier":
        r = 6.0
        _circle = page.add_circle_annot(pymupdf.Rect(a.x - r, a.y - r, a.x + r, a.y + r))
        _circle.set_border(width=1, dashes=[2, 2])
        _circle.set_colors(stroke=RED)
        _info(_circle, "Pier", content)
        _circle.update()
        if e.tag:
            _label(page, pymupdf.Point(a.x + r + 1, a.y - r + 2), e.tag, 9, RED, "Pier Tag")
    elif e.kind == "column":
        _rect(page, pymupdf.Rect(a.x - 2.5, a.y - 2.5, a.x + 2.5, a.y + 2.5), colour, 0.5, "Column", fill=colour)
        if e.tag:
            _label(page, pymupdf.Point(a.x + 4, a.y - 3), e.tag, 11, colour, "Column Tag")
    elif e.kind == "cloud":
        r = pymupdf.Rect(a, b).normalize()
        if r.width < 8 or r.height < 8:
            r = pymupdf.Rect(a.x - 12, a.y - 12, a.x + 12, a.y + 12)
        _rect(page, r, RED, 1.5, "Cloud", clouds=2)
        if e.tag or e.text:
            _label(page, pymupdf.Point(r.x1 + 2, r.y0), e.text or e.tag, 8, RED, "Cloud Tag")
    elif e.kind == "tag":
        _label(page, a, e.text or e.tag, 11 if e.tag and not e.text else 8, colour, "Tag")


# ---------------------------------------------------------------------------
# Callouts: boxed notes in the margins with arrow leaders, like the examples
# ---------------------------------------------------------------------------

CALLOUT_FS = 6.5
CALLOUT_W = 150.0


class _Band:
    def __init__(self, rect: pymupdf.Rect, horizontal: bool) -> None:
        self.rect, self.horizontal = rect, horizontal
        self.cursor = rect.x0 if horizontal else rect.y0
        self.row_y = rect.y0
        self.row_h = 0.0

    def place(self, anchor: pymupdf.Point, w: float, h: float) -> pymupdf.Rect | None:
        r = self.rect
        if self.horizontal:
            x = max(self.cursor, anchor.x - w / 2)
            if x + w > r.x1:  # new row
                self.row_y += self.row_h + 4
                self.row_h, self.cursor = 0.0, r.x0
                x = max(self.cursor, min(anchor.x - w / 2, r.x1 - w))
            if self.row_y + h > r.y1 or x + w > r.x1:
                return None
            box = pymupdf.Rect(x, self.row_y, x + w, self.row_y + h)
            self.cursor, self.row_h = box.x1 + 6, max(self.row_h, h)
            return box
        y = max(self.cursor, anchor.y - h / 2)
        if y + h > r.y1:
            return None
        box = pymupdf.Rect(r.x0, y, r.x0 + w, y + h)
        self.cursor = box.y1 + 5
        return box


def _draw_callouts(page: pymupdf.Page, callouts: list[Element], m: PlanMap) -> list[str]:
    top, right, bottom = _Band(BAND_TOP, True), _Band(BAND_RIGHT, False), _Band(BAND_BOTTOM, True)
    centre_y = (m.placed.y0 + m.placed.y1) / 2
    right_x = m.placed.x1 - m.placed.width * 0.25
    groups: dict[int, list] = {0: [], 1: [], 2: []}
    bands = [top, right, bottom]
    for e in callouts:
        a = m(e.start)
        text = (e.text or e.tag).upper()
        lines = _wrap(text, CALLOUT_W - 8, CALLOUT_FS)
        h = _text_height(len(lines), CALLOUT_FS)
        w = min(CALLOUT_W, max(_text_width(ln, CALLOUT_FS) for ln in lines) + 10)
        side = 1 if a.x > right_x else (0 if a.y < centre_y else 2)
        groups[side].append((a, text, lines, w, h))
    groups[0].sort(key=lambda t: t[0].x)
    groups[1].sort(key=lambda t: t[0].y)
    groups[2].sort(key=lambda t: t[0].x)
    fallback = {0: [1, 2], 1: [0, 2], 2: [1, 0]}
    unplaced: list[str] = []
    for side in (0, 1, 2):
        for a, text, lines, w, h in groups[side]:
            box = None
            for b in [side, *fallback[side]]:
                box = bands[b].place(a, w, h)
                if box:
                    break
            if box is None:
                unplaced.append(text)
                continue
            if box.x0 <= a.x <= box.x1:
                attach = pymupdf.Point(min(max(a.x, box.x0 + 4), box.x1 - 4), box.y1 if a.y > box.y1 else box.y0)
            else:
                attach = pymupdf.Point(box.x0 if a.x < box.x0 else box.x1, (box.y0 + box.y1) / 2)
            _text(page, box, "\n".join(lines), CALLOUT_FS, RED, "Call-out", align=1, border=0.5,
                  fill=(1, 1, 1), callout=[a, attach])
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


def _new_sheet(out: pymupdf.Document, meta: SheetMeta, title: str, dwg_no: str) -> pymupdf.Page:
    page = out.new_page(width=A3.width, height=A3.height)
    _title(page, meta.project, title)
    _title_block(page, meta.project, title, dwg_no, meta.job_no, meta.rev, meta.made_by, meta.date)
    return page


def _markup_sheet(out, src, meta: SheetMeta, kind: SheetKind, sheet: MarkupSheet, dwg_no: str) -> None:
    page = _new_sheet(out, meta, SHEET_TITLES[kind], dwg_no)
    idx = min(max(sheet.source_page_number, 1), src.page_count) - 1
    m = PlanMap(src[idx], sheet.plan_region)
    m.draw_background(page, src)
    callouts: list[Element] = []
    off_plan: list[str] = []
    for e in sheet.elements:
        on_plan = m.inside(e.start) and m.inside(e.end)
        if e.kind == "callout":
            if on_plan:
                callouts.append(e)
            else:
                off_plan.append(e.text or e.tag)
        elif on_plan:
            _draw_element(page, e, m)
    unplaced = _draw_callouts(page, callouts, m) + [t.upper() for t in off_plan if t]
    notes = list(sheet.notes) + [f"-{t}" for t in unplaced]  # never drop a note
    _notes(page, sheet.classification, notes)
    _schedule(page, sheet.schedule_title, sheet.schedule, sheet.legend_title, sheet.legend)


def _summary_sections(geotech: GeotechReview, d: DesignAssessment, references: list[str]) -> list[tuple[str, list[str]]]:
    g = geotech
    return [
        ("PROJECT", [d.project_summary, "", "REFERENCES SUPPLIED:", *([f"- {r}" for r in references] or ["- none"])]),
        ("1. GEOTECHNICAL & WIND", [
            f"REPORT PROVIDED: {'YES' if g.report_provided else 'NO'}",
            f"AS2870 SITE CLASS: {g.site_classification}   ys: {g.ys}",
            f"BEARING: {g.bearing_capacity}",
            f"FOUNDING: {g.founding}",
            f"FILL: {g.fill}",
            f"GROUNDWATER: {g.groundwater}",
            f"TREES / MOISTURE: {g.trees_or_moisture}",
            f"AS4055 WIND CLASS: {g.wind_classification} ({g.wind_source})",
            "RECOMMENDATIONS:", *[f"- {x}" for x in g.recommendations],
            "CONCERNS:", *[f"- {x}" for x in g.concerns],
        ]),
        ("2. ROOF / WALLS / FLOOR", [
            f"ROOF: {d.roof_form}; {d.roof_construction}; PITCH {d.roof_pitch}; {d.roof_cladding}",
            *[f"- DECK: {x}" for x in d.deck_areas],
            f"WALLS: EXT {d.external_wall_type}; INT {d.internal_wall_type}",
            f"FRAMING: {d.framing_material} TO {d.governing_standard}; {d.storeys} STOREY(S); HEIGHT {d.wall_height}",
            f"FLOOR: {d.floor_system} - {d.floor_description}",
        ]),
        ("3. LOAD PATHS", [f"- {s.element}: {s.carries} -> {s.transfers_to}" for s in d.load_paths]),
        ("4. FOOTINGS (AS2870)", [
            f"{d.footing_system} (SITE CLASS {d.footing_site_class_used})",
            f"BASIS: {d.footing_as2870_basis}",
            *[f"- {x}" for x in d.footing_details],
        ]),
        ("5. WALL DESIGN / BRACING", [
            f"WIND: {d.wind_classification_used}",
            d.racking_summary,
            *[f"- {b.location}: {b.bracing_type} ({b.capacity})" for b in d.bracing_walls],
            f"TIE-DOWN: {d.tie_down}",
            *[f"- LINTEL/BEAM: {x}" for x in d.lintels_and_beams],
            *[f"- JOINT: {x}" for x in d.articulation_and_control_joints],
        ]),
        ("ASSUMPTIONS", [f"- {x}" for x in d.assumptions]),
        ("ENGINEER TO CHECK", [f"- {x}" for x in d.engineer_checks]),
    ]


def _summary_sheet(out, meta: SheetMeta, geotech, design, references) -> None:
    page = _new_sheet(out, meta, "STRUCTURAL REVIEW SUMMARY", "SSK-0")
    cols = [pymupdf.Rect(15, 95, 395, 832), pymupdf.Rect(405, 95, 785, 676), pymupdf.Rect(795, 95, 1180, 676)]
    sections = _summary_sections(geotech, design, references)
    _text(page, pymupdf.Rect(800, 20, 1180, 70), "PRELIMINARY - FOR ENGINEER REVIEW.\nNOT FOR CONSTRUCTION.", 12, RED, "Status")
    fs = 7.5
    col, y = 0, cols[0].y0
    for heading, body in sections:
        text = heading + "\n" + "\n".join(body).upper()
        lines = _wrap(text, cols[col].width - 8, fs)
        h = _text_height(len(lines), fs)
        while y + h > cols[col].y1 and col < len(cols) - 1:
            col += 1
            y = cols[col].y0
        r = pymupdf.Rect(cols[col].x0, y, cols[col].x1, min(y + h, cols[col].y1))
        _text(page, r, "\n".join(lines), fs, BLACK, "Summary")
        y += h + 6


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
) -> bytes:
    src = pymupdf.open(stream=arch_pdf, filetype="pdf")
    out = pymupdf.open()
    meta = SheetMeta(project, job_no, rev, made_by, dt.date.today().strftime("%d/%m/%Y"))
    _summary_sheet(out, meta, geotech, design, references)
    n = 1
    for kind in SheetKind:
        if kind in sheets:
            _markup_sheet(out, src, meta, kind, sheets[kind], f"SSK-{n}")
            n += 1
    return out.tobytes(garbage=3, deflate=True)
