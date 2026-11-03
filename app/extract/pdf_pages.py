"""Step 1 - what is on each page, from text alone.

For every page: the text with positions, whether it is vector or a scanned image,
the sheet size, every scale mention (`1:100`, `1 : 100 @ A2`, `Scale @ A3: 1:100`)
with its position, a page-kind guess from the title and the title-block key/values
(`Soil Classification`, `Wind Speed`, `BAL`, `Project`, `Drawing Title`, ...).

Coordinates are PDF user space (points, y up) like the rest of the package.

Failure modes: a scale printed as an image or as outlined glyphs is not text and is
missed (the dimension check in `dimensions.py` covers that); a title block laid out
in a way we have not seen (value far from its label) yields no key/values; an
unusual title ("GF LAYOUT") is classed `other` and the caller can still pick the
page by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

from .vector import page_to_pdf

ISO_SHEETS = {"A4": (595, 842), "A3": (842, 1191), "A2": (1191, 1684), "A1": (1684, 2384), "A0": (2384, 3370)}
STANDARD_SCALES = {1, 2, 5, 10, 20, 25, 50, 100, 125, 200, 250, 500, 1000, 1250, 2000, 2500, 5000}

Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class Word:
    text: str
    bbox: Rect  # y-up
    vertical: bool
    size: float
    line_index: int

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2)


@dataclass(frozen=True)
class Line:
    text: str
    bbox: Rect  # y-up
    vertical: bool
    size: float


@dataclass(frozen=True)
class ScaleMention:
    denominator: float
    sheet: str | None  # "A2" when written "1:100 @ A2"
    bbox: Rect
    text: str


@dataclass
class PageScan:
    number: int  # 1-based
    width: float
    height: float
    sheet_size: str
    is_vector: bool
    drawing_count: int
    raster_cover: float
    lines: list[Line] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    scales: list[ScaleMention] = field(default_factory=list)
    kind: str = "other"
    title: str = ""
    title_block: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


def _to_pdf_rect(r, m: pymupdf.Matrix) -> Rect:
    rr = pymupdf.Rect(r) * m
    return (min(rr.x0, rr.x1), min(rr.y0, rr.y1), max(rr.x0, rr.x1), max(rr.y0, rr.y1))


def read_text(page: pymupdf.Page) -> tuple[list[Line], list[Word]]:
    m = page_to_pdf(page)
    d = page.get_text("dict")
    lines: list[Line] = []
    line_dirs: dict[tuple[int, int], tuple[bool, float]] = {}
    for bi, block in enumerate(d["blocks"]):
        for li, ln in enumerate(block.get("lines", [])):
            text = "".join(s["text"] for s in ln["spans"]).strip()
            if not text:
                continue
            vertical = abs(ln["dir"][1]) > abs(ln["dir"][0])
            size = max((s["size"] for s in ln["spans"]), default=0.0)
            line_dirs[(bi, li)] = (vertical, size)
            lines.append(Line(text, _to_pdf_rect(ln["bbox"], m), vertical, size))
    words: list[Word] = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text, bno, lno, _ = w
        vertical, size = line_dirs.get((bno, lno), (False, 0.0))
        if not size:  # fall back on the shape of the box
            vertical = (y1 - y0) > 1.5 * (x1 - x0) and len(text) >= 2
            size = (x1 - x0) if vertical else (y1 - y0)
        words.append(Word(text, _to_pdf_rect((x0, y0, x1, y1), m), vertical, size, len(lines)))
    return lines, words


# ---------------------------------------------------------------------------
# scale, sheet, kind
# ---------------------------------------------------------------------------

_SCALE_RE = re.compile(r"1\s*:\s*(\d{1,4})(?:\s*@\s*(A\d))?")


def find_scales(lines: list[Line]) -> list[ScaleMention]:
    out: list[ScaleMention] = []
    for ln in lines:
        for mt in _SCALE_RE.finditer(ln.text):
            den = int(mt.group(1))
            if den not in STANDARD_SCALES:
                continue  # "1:60 - 1:80 fall to waste" is a gradient, not a scale
            out.append(ScaleMention(float(den), mt.group(2), ln.bbox, mt.group(0)))
    return out


def sheet_size_for(width: float, height: float) -> str:
    short, long_ = sorted((width, height))
    for name, (s, l) in ISO_SHEETS.items():
        if abs(short - s) / s < 0.03 and abs(long_ - l) / l < 0.03:
            return name
    return f"{round(width / 72 * 25.4)}x{round(height / 72 * 25.4)}mm"


_KIND_RULES: list[tuple[str, str]] = [
    (r"\bCOVER\b|DRAWING (LIST|INDEX|SCHEDULE)", "cover"),
    (r"SITE PLAN|LOCATION PLAN|SETOUT PLAN|SET-OUT PLAN", "site_plan"),
    (r"ROOF PLAN|ROOF LAYOUT|ROOF FRAMING", "roof_plan"),
    (r"DRAINAGE|STORMWATER|SEWER|PLUMBING|SWMP", "drainage"),
    (r"ELECTRICAL|LIGHTING PLAN", "electrical"),
    (r"ELEVATION", "elevation"),
    (r"\bSECTION", "section"),
    (r"FLOOR PLAN|GROUND FLOOR|FIRST FLOOR|UPPER FLOOR|LOWER FLOOR|\bPLAN - UNIT|FLOOR LAYOUT", "floor_plan"),
    (r"DETAIL", "detail"),
    (r"SCHEDULE", "schedule"),
    (r"\bNOTES\b|SPECIFICATION", "notes"),
]


_TITLE_WORDS = re.compile("|".join(p for p, _ in _KIND_RULES) + r"|SLAB|FOOTING|BRACING|LINTEL|FRAMING|LAYOUT")


def guess_kind(title: str, lines: list[Line]) -> str:
    """Kind from the drawing title; if that gives nothing, from the biggest text."""
    for pattern, kind in _KIND_RULES:
        if re.search(pattern, title.upper()):
            return kind
    big = sorted(lines, key=lambda ln: -ln.size)[:12]
    for pattern, kind in _KIND_RULES:
        for ln in big:
            if re.search(pattern, ln.text.upper()):
                return kind
    return "other"


# ---------------------------------------------------------------------------
# title block key / values
# ---------------------------------------------------------------------------

# canonical key -> regex on the label with spaces removed and lower-cased
_TITLE_KEYS: list[tuple[str, str]] = [
    ("soil_classification", r"^(soil|site)class(ification)?:?$"),
    ("wind_class", r"^wind(speed|class(ification)?|rating|load)?:?$"),
    ("bal", r"^(certified)?bal:?$"),
    ("project", r"^(client/)?project(info)?:?$"),
    ("drawing_title", r"^drawingtitle:?$"),
    ("page_number", r"^(pagenumber|sheet|sheetno\.?|drawingno\.?|dwgno\.?):?$"),
    ("scale", r"^scale(@a\d)?:?$"),
    ("client", r"^client:?$"),
    ("job_no", r"^(jobnumber|jobno\.?|job):?$"),
    ("date", r"^date:?$"),
    ("climate_zone", r"^climatezone:?$"),
    ("corrosion_environment", r"^corrosionenvironment:?$"),
    ("title_reference", r"^titlereference:?$"),
    ("drawn_by", r"^drawn(by)?:?$"),
]


def _norm_label(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _value_for(label: Line, lines: list[Line]) -> str:
    """Value is either the nearest text to the right on the same row, or the line
    immediately below the label when that line is left-aligned with it and is not
    itself a label. Both layouts occur (Maveric: below; Another Perspective: right).
    When both exist the closer one wins."""
    lx0, ly0, lx1, ly1 = label.bbox
    h = max(ly1 - ly0, 4.0)
    cy = (ly0 + ly1) / 2
    right: tuple[float, str] | None = None
    below: tuple[float, Line] | None = None
    for ln in lines:
        if ln is label or ln.vertical:
            continue
        same_row = abs((ln.bbox[1] + ln.bbox[3]) / 2 - cy) < h * 0.8 and ln.bbox[0] >= lx1 - 1
        if same_row and ln.bbox[0] - lx1 < 220 and not _is_label(ln.text):
            d = ln.bbox[0] - lx1
            if right is None or d < right[0]:
                right = (d, ln.text.strip())
            continue
        gap = ly0 - ln.bbox[3]
        if -1 < gap < h * 2.5 and ln.bbox[0] < lx1 + 40 and ln.bbox[2] > lx0 - 5:
            if below is None or gap < below[0]:
                below = (gap, ln)
    below_ok = below is not None and not _is_label(below[1].text) and abs(below[1].bbox[0] - lx0) <= 8
    if below_ok and (right is None or below[0] < right[0]):
        return below[1].text.strip()
    return right[1] if right else ""


def _is_label(text: str) -> bool:
    norm = _norm_label(text)
    return norm.endswith(":") or any(re.match(p, norm) for _, p in _TITLE_KEYS)


def in_title_block_region(bbox: Rect, width: float, height: float) -> bool:
    """Title blocks live along the bottom or right edge of the sheet."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return cy < height * 0.22 or cx > width * 0.78


def read_title_block(lines: list[Line], width: float, height: float) -> dict[str, str]:
    found: dict[str, str] = {}
    for ln in lines:
        if ln.vertical or len(ln.text) > 32 or not in_title_block_region(ln.bbox, width, height):
            continue
        norm = _norm_label(ln.text)
        for key, pattern in _TITLE_KEYS:
            if key in found or not re.match(pattern, norm):
                continue
            # label and value in one line ("Scale 1:100")?
            value = _value_for(ln, lines)
            if value and not _norm_label(value).endswith(":"):
                found[key] = value
    return found


# ---------------------------------------------------------------------------
# page scan
# ---------------------------------------------------------------------------


def _raster_cover(page: pymupdf.Page) -> float:
    area = page.rect.width * page.rect.height
    best = 0.0
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"]) & page.rect
            best = max(best, (r.width * r.height) / area if area else 0.0)
    except Exception:  # pragma: no cover - defensive; image info is optional
        pass
    return best


def scan_page(page: pymupdf.Page, drawing_count: int | None = None) -> PageScan:
    if drawing_count is None:
        drawing_count = len(page.get_drawings())
    lines, words = read_text(page)
    cover = _raster_cover(page)
    is_vector = drawing_count >= 30 and not (cover > 0.6 and drawing_count < 200)
    scan = PageScan(
        number=page.number + 1,
        width=page.rect.width,
        height=page.rect.height,
        sheet_size=sheet_size_for(page.rect.width, page.rect.height),
        is_vector=is_vector,
        drawing_count=drawing_count,
        raster_cover=cover,
        lines=lines,
        words=words,
    )
    scan.scales = find_scales(lines)
    scan.title_block = read_title_block(lines, scan.width, scan.height)
    scan.title = scan.title_block.get("drawing_title", "")
    if not scan.title:
        # biggest text in the title block that names a drawing kind, else anywhere
        in_block = [ln for ln in lines if in_title_block_region(ln.bbox, scan.width, scan.height)]

        def rank(ln: Line) -> tuple[float, float]:  # biggest first, then nearest the bottom-right corner
            return (-round(ln.size, 1), (scan.width - ln.bbox[2]) + ln.bbox[1])

        for pool in (in_block, lines):
            for ln in sorted(pool, key=rank):
                if _TITLE_WORDS.search(ln.text.upper()):
                    scan.title = ln.text
                    break
            if scan.title:
                break
    scan.kind = guess_kind(scan.title, lines)
    if scan.number == 1 and scan.kind == "other" and re.search(r"COVER|DRAWING LIST|SHEET LIST", scan.text.upper()):
        scan.kind = "cover"
    return scan


def title_block_scale(scan: PageScan) -> ScaleMention | None:
    """The scale written in the title block (label 'Scale'), if any."""
    value = scan.title_block.get("scale", "")
    mt = _SCALE_RE.search(value)
    if not mt or int(mt.group(1)) not in STANDARD_SCALES:
        return None
    for s in scan.scales:
        if s.denominator == float(mt.group(1)):
            return s
    return ScaleMention(float(mt.group(1)), mt.group(2), (0, 0, 0, 0), value)
