"""Check the lintels on a wall framing sheet against the architect's openings.

1. Missing lintels: every window / door the architect labelled with a size code
   (A1812, SD2121, F2103 ... height then width, in hundreds of mm) is an opening in
   an external wall. If it is on a wall line that has other lintels (so the wall is
   load-bearing and in scope) but no lintel covers it, it is flagged.
2. Size consistency: lintel sizes are read from the sheet's schedule
   ('2/140x45 F17 ...'). On the same wall line (similar loading), a lintel over a
   wider opening must not be smaller than one over a narrower opening.
3. Span / depth sanity: a single lintel spanning more than MAX_SPAN_DEPTH times
   its depth is flagged. Continuous lintels are left out of 2 and 3.

Flags become a red cloud on the sheet (an editable annotation the engineer can
delete) plus a warning. This is a consistency check only - it does not size
lintels against AS 1684.2.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import pymupdf

from .schemas import Element, MarkupSheet, Point
from .snap import Geometry, _solid, mm_to_pt

CODE = re.compile(r"^(SD|SL|SW|AW|A|DH|D|FD|FG|F|W|BF|GD|CS|S)(\d{2})(\d{2})$")
# Single timber lintels on the engineers' sheets span at most ~17.5x their depth
# (120 deep over 2100 on a lightly loaded wall); flag anything well beyond that.
MAX_SPAN_DEPTH = float(os.environ.get("LINTEL_MAX_SPAN_DEPTH", "20"))
SIZE = re.compile(r"(?:(\d)\s*/\s*)?(\d{2,3})\s*[xX×]\s*(\d{2,3})")


@dataclass
class Opening:
    code: str
    width_mm: int
    horizontal: bool  # wall direction
    c: float  # wall centreline
    a0: float  # opening extent along the wall (page pt)
    a1: float


def _runs(geo: Geometry, horizontal: bool) -> list[tuple[float, float, float, float]]:
    """All wall runs (c, lo, hi, thickness) in one direction: wall pieces on the same
    centreline joined across gaps <= 3 pt. Unfiltered, so perimeter walls next to
    eaves / fascia lines are kept."""
    pieces = sorted(geo.walls[0] if horizontal else geo.walls[1], key=lambda w: (round(w[0]), w[1]))
    runs: list[list[float]] = []
    for c, lo, hi, t in pieces:
        for r in runs:
            if abs(r[0] - c) <= 1.0 and lo <= r[2] + 3 and hi >= r[1] - 3:
                r[1], r[2], r[3] = min(r[1], lo), max(r[2], hi), max(r[3], t)
                break
        else:
            runs.append([c, lo, hi, t])
    runs = [r for r in runs if r[2] - r[1] >= 15]
    # Decking / stair treads: many parallels a few pt apart that start and end
    # together. (Perimeter walls and the eaves lines beside them don't line up.)
    def boards(r: list[float]) -> int:
        return sum(1 for v in runs if v is not r and abs(v[0] - r[0]) <= 8
                   and abs(v[1] - r[1]) <= 5 and abs(v[2] - r[2]) <= 5)
    return [tuple(r) for r in runs if boards(r) < 3]


def _code_labels(page: pymupdf.Page) -> list[tuple[str, float, float, bool]]:
    """(code, x, y, text is horizontal) for every window / door size code."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            horizontal = abs(line["dir"][0]) >= abs(line["dir"][1])
            for span in line["spans"]:
                for word in span["text"].split():
                    if CODE.match(word):
                        b = span["bbox"]
                        out.append((word, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, horizontal))
    return out


def find_openings(page: pymupdf.Page, geo: Geometry, scale: int) -> list[Opening]:
    """Coded openings, located on the wall they label. A label's text direction
    gives its wall's direction (rotated labels sit beside vertical walls)."""
    out = []
    runs = {True: _runs(geo, True), False: _runs(geo, False)}
    for code, lx, ly, horizontal in _code_labels(page):
        width_mm = int(CODE.match(code).group(3)) * 100
        if width_mm < 300:
            continue
        width_pt = mm_to_pt(width_mm, scale)
        along, across = (lx, ly) if horizontal else (ly, lx)
        near = sorted((abs(across - c), c, lo, hi, t) for c, lo, hi, t in runs[horizontal]
                      if lo - 5 <= along <= hi + 5 and abs(across - c) <= 25)
        if not near:
            continue
        found = None
        for _, c, lo, hi, t in near:  # nearest wall that has an opening of this width here
            solid = _solid(geo, horizontal, c, t, lo, hi)
            if sum(e - s for s, e in solid) < 0.4 * (hi - lo):
                continue
            gaps = [(solid[i][1], solid[i + 1][0]) for i in range(len(solid) - 1)]
            gaps = [g for g in gaps if 0.5 * width_pt <= g[1] - g[0] <= 1.6 * width_pt
                    and abs((g[0] + g[1]) / 2 - along) <= max(20.0, width_pt)]
            if gaps:
                g = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - along))
                found = (c, g[0], g[1])
                break
        if found:
            c, a0, a1 = found
        else:  # fallback: nearest wall, opening centred on the label
            c = near[0][1]
            a0, a1 = along - width_pt / 2, along + width_pt / 2
        out.append(Opening(code, width_mm, horizontal, c, a0, a1))
    return out


def _size(text: str) -> tuple[int, int, int] | None:
    m = SIZE.search(text or "")
    if not m:
        return None
    return int(m.group(1) or 1), int(m.group(2)), int(m.group(3))


def _fmt(size: tuple[int, int, int]) -> str:
    n, d, b = size
    return f"{n}/{d}x{b}" if n > 1 else f"{d}x{b}"


def _strength(size: tuple[int, int, int]) -> float:
    n, d, b = size  # number, depth, breadth: bending capacity ~ n * b * d^2
    return n * b * d * d


def check_lintels(page: pymupdf.Page, geo: Geometry, scale: int | None, sheet: MarkupSheet, title: str) -> list[str]:
    """Adds clouds for missing / inconsistent lintels to the sheet; returns warnings."""
    if scale is None or not geo.usable:
        return []
    w, h = page.rect.width, page.rect.height
    openings = find_openings(page, geo, scale)
    if not openings:
        return []
    lintels = []
    for e in sheet.elements:
        if e.kind == "member" and re.match(r"\s*L\d", e.tag or "", re.I):
            p = pymupdf.Point(e.start.x * w, e.start.y * h)
            q = pymupdf.Point(e.end.x * w, e.end.y * h)
            horizontal = abs(q.x - p.x) >= abs(q.y - p.y)
            lo, hi = sorted((p.x, q.x) if horizontal else (p.y, q.y))
            lintels.append((e.tag.strip().upper(), horizontal, (p.y + q.y) / 2 if horizontal else (p.x + q.x) / 2, lo, hi))
    sizes = {i.tag.strip().upper(): _size(i.text) for i in sheet.schedule}
    continuous = {i.tag.strip().upper() for i in sheet.schedule if re.search(r"CONTIN", i.text or "", re.I)}

    def cloud(o: Opening, text: str) -> None:
        pad = 6.0
        if o.horizontal:
            box = (o.a0 - pad, o.c - pad, o.a1 + pad, o.c + pad)
        else:
            box = (o.c - pad, o.a0 - pad, o.c + pad, o.a1 + pad)
        sheet.elements.append(Element(kind="cloud", tag="", start=Point(x=box[0] / w, y=box[1] / h),
                                      end=Point(x=box[2] / w, y=box[3] / h), text=text))

    warnings: list[str] = []
    covered: list[tuple[str, Opening]] = []
    missing: list[Opening] = []
    for o in openings:
        hit = None
        for tag, horizontal, c, lo, hi in lintels:
            overlap = min(hi, o.a1) - max(lo, o.a0)
            if horizontal == o.horizontal and abs(c - o.c) <= 8 and overlap >= 0.5 * (o.a1 - o.a0):
                hit = tag
                break
        if hit:
            covered.append((hit, o))
        elif any(horizontal == o.horizontal and abs(c - o.c) <= 1.5 for _, horizontal, c, _, _ in lintels):
            # Only on walls already given a lintel: they are in the structural scope and
            # load-bearing. (On alteration jobs the existing walls carry no mark-ups.)
            missing.append(o)
    for o in missing:
        cloud(o, "LINTEL REQUIRED? CHECK")
    if missing:
        warnings.append(f"{title}: opening(s) with no lintel on a wall that has other lintels - check "
                        "(ignore any in existing walls outside the scope of works): "
                        + ", ".join(f"{o.code} ({o.width_mm} wide)" for o in missing))

    # Consistency, on the same wall line only (similar loading): a wider opening must
    # not get a smaller lintel than a narrower one. Continuous lintels are skipped.
    flagged = set()
    for tag_a, oa in covered:
        sa = sizes.get(tag_a)
        if not sa or tag_a in continuous:
            continue
        for tag_b, ob in covered:
            sb = sizes.get(tag_b)
            if not sb or oa is ob or tag_b in continuous:
                continue
            same_wall = oa.horizontal == ob.horizontal and abs(oa.c - ob.c) <= 1.5
            if same_wall and oa.width_mm >= ob.width_mm + 200 and _strength(sa) < _strength(sb) and id(oa) not in flagged:
                flagged.add(id(oa))
                cloud(oa, f"CHECK {tag_a} SIZE")
                warnings.append(f"{title}: {tag_a} ({_fmt(sa)}) over {oa.code} ({oa.width_mm} wide) is smaller than "
                                f"{tag_b} ({_fmt(sb)}) over {ob.code} ({ob.width_mm} wide) on the same wall - check {tag_a}")

    # Sanity: clear span more than MAX_SPAN_DEPTH x the lintel depth.
    for tag, o in covered:
        sz = sizes.get(tag)
        if not sz or tag in continuous or id(o) in flagged:
            continue
        ratio = o.width_mm / sz[1]
        if ratio > MAX_SPAN_DEPTH:
            flagged.add(id(o))
            cloud(o, f"CHECK {tag} SIZE")
            warnings.append(f"{title}: {tag} ({_fmt(sz)}) over {o.code} ({o.width_mm} wide) spans {ratio:.0f}x its "
                            f"depth (flag above {MAX_SPAN_DEPTH:g}) - check {tag} against the AS 1684.2 lintel tables")
    return warnings
