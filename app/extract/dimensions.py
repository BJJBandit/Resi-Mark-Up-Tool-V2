"""Step 3 - dimension strings, their tick marks, chains, and the scale check.

A dimension on an architectural plan is a number (mm) sitting just off a thin
dimension line, between two tick marks. Measuring the tick spacing in points and
dividing by the labelled millimetres gives points-per-millimetre, which must equal
72 / 25.4 / scale. Many dimensions on a page vote; the biggest cluster of agreeing
votes is the page's measured scale, and that confirms (or replaces) the scale read
from the text.

Failure modes: dimensions drawn as outlined glyphs are not text and are not seen;
a number whose dimension line is crossed by other geometry gets a wrong interval
and becomes an outlier (ignored by the cluster, but reduces confidence); a page
with details at a second scale produces a second cluster, which is reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from .geom import PT_PER_MM_PAPER, Axis, Seg
from .pdf_pages import PageScan, Word, in_title_block_region
from .vector import VectorSet

_NUM_RE = re.compile(r"^\d{2,5}$")
MIN_DIM_MM = 50
MAX_DIM_MM = 80_000
TICK_MIN_PT, TICK_MAX_PT = 1.5, 14.0
CLUSTER_REL_TOL = 0.015  # ratios within 1.5 % of each other agree


@dataclass
class Dim:
    value_mm: float
    axis: Axis  # direction the dimension measures along
    pos: float  # position of the dimension line across the axis
    lo: float  # tick interval along the axis (points)
    hi: float
    word: Word
    consistent: bool = False

    @property
    def span_pt(self) -> float:
        return self.hi - self.lo

    @property
    def ratio(self) -> float:
        return self.span_pt / self.value_mm

    @property
    def centre(self) -> tuple[float, float]:
        return self.word.centre


@dataclass
class Chain:
    axis: Axis
    pos: float
    dims: list[Dim]
    ticks: list[float]
    overall: Dim | None = None  # a single dimension spanning the whole chain, if drawn
    sum_mismatch_pct: float | None = None


@dataclass
class DimensionResult:
    page_number: int
    dims: list[Dim] = field(default_factory=list)
    chains: list[Chain] = field(default_factory=list)
    ratio_pt_per_mm: float | None = None  # measured
    scale_measured: float | None = None  # denominator, e.g. 100.0
    n_votes: int = 0
    n_agree: int = 0
    secondary_scales: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        if self.n_votes == 0:
            return 0.0
        return min(1.0, self.n_agree / self.n_votes) * min(1.0, self.n_agree / 6)

    def extents_mm(self, axis: Axis) -> float | None:
        vals = [d.value_mm for d in self.dims if d.consistent and d.axis == axis]
        return max(vals) if vals else None


# ---------------------------------------------------------------------------


def numeric_words(scan: PageScan) -> list[Word]:
    out = []
    for w in scan.words:
        if not _NUM_RE.match(w.text):
            continue
        v = int(w.text)
        if v < MIN_DIM_MM or v > MAX_DIM_MM:
            continue
        if in_title_block_region(w.bbox, scan.width, scan.height) and v < 1000:
            continue  # phone numbers and dates in the title block
        out.append(w)
    return out


def _text_geometry(w: Word) -> tuple[Axis, float, float, float]:
    """(axis measured, centre along axis, centre across axis, text thickness across)."""
    x0, y0, x1, y1 = w.bbox
    if w.vertical:
        return "v", (y0 + y1) / 2, (x0 + x1) / 2, (x1 - x0)
    return "h", (x0 + x1) / 2, (y0 + y1) / 2, (y1 - y0)


def _dimension_line(w: Word, segs: list[Seg]) -> Seg | None:
    axis, along, across, thick = _text_geometry(w)
    best: tuple[float, Seg] | None = None
    for s in segs:
        if s.axis != axis or s.kind != "stroke" or s.length < 6:
            continue
        if not (s.lo - 2 <= along <= s.hi + 2):
            continue
        d = abs(s.pos - across)
        if thick / 2 - 1.0 <= d <= thick * 1.6:
            if best is None or d < best[0]:
                best = (d, s)
    return best[1] if best else None


def _crossing(seg: Seg, line: Seg) -> float | None:
    """Where a short segment meets the dimension line, as a coordinate along the
    line, or None if it does not come within 0.7 pt of it."""
    if line.axis == "h":
        a0, a1, c0, c1 = seg.x0, seg.x1, seg.y0, seg.y1
    else:
        a0, a1, c0, c1 = seg.y0, seg.y1, seg.x0, seg.x1
    p = line.pos
    if abs(c1 - c0) < 1e-6:
        return None  # parallel to the line
    if min(c0, c1) - 0.7 <= p <= max(c0, c1) + 0.7:
        t = (p - c0) / (c1 - c0)
        t = min(1.0, max(0.0, t))
        return a0 + t * (a1 - a0)
    return None


def _ticks(line: Seg, segs: list[Seg]) -> list[float]:
    raw = [line.lo, line.hi]
    for s in segs:
        if s is line or not (TICK_MIN_PT <= s.length <= TICK_MAX_PT):
            continue
        b = s.bbox()
        if line.axis == "h":
            if b[2] < line.lo - 1 or b[0] > line.hi + 1 or b[1] > line.pos + 1 or b[3] < line.pos - 1:
                continue
        else:
            if b[3] < line.lo - 1 or b[1] > line.hi + 1 or b[0] > line.pos + 1 or b[2] < line.pos - 1:
                continue
        x = _crossing(s, line)
        if x is not None:
            raw.append(x)
    raw.sort()
    ticks: list[list[float]] = []
    for x in raw:
        if ticks and x - ticks[-1][-1] <= 1.2:
            ticks[-1].append(x)
        else:
            ticks.append([x])
    return [sum(t) / len(t) for t in ticks]


def _interval(ticks: list[float], along: float) -> tuple[float, float] | None:
    for a, b in zip(ticks, ticks[1:]):
        if a - 0.5 <= along <= b + 0.5 and b - a > 0.5:
            return a, b
    return None


def _largest_cluster(values: list[float]) -> list[float]:
    if not values:
        return []
    vals = sorted(values)
    best: list[float] = []
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] <= vals[i] * (1 + 2 * CLUSTER_REL_TOL):
            j += 1
        if j - i + 1 > len(best):
            best = vals[i : j + 1]
        i += 1
    return best


# ---------------------------------------------------------------------------


def analyse_dimensions(scan: PageScan, vectors: VectorSet) -> DimensionResult:
    res = DimensionResult(page_number=scan.number)
    segs = vectors.segments
    for w in numeric_words(scan):
        line = _dimension_line(w, segs)
        if line is None:
            continue
        axis, along, _, _ = _text_geometry(w)
        iv = _interval(_ticks(line, segs), along)
        if iv is None:
            continue
        res.dims.append(Dim(float(w.text), axis, line.pos, iv[0], iv[1], w))

    ratios = [d.ratio for d in res.dims]
    res.n_votes = len(ratios)
    cluster = _largest_cluster(ratios)
    if len(cluster) >= 2:
        res.ratio_pt_per_mm = median(cluster)
        res.scale_measured = PT_PER_MM_PAPER / res.ratio_pt_per_mm
        for d in res.dims:
            d.consistent = abs(d.ratio - res.ratio_pt_per_mm) <= res.ratio_pt_per_mm * CLUSTER_REL_TOL
        res.n_agree = sum(1 for d in res.dims if d.consistent)
        rest = [d.ratio for d in res.dims if not d.consistent]
        second = _largest_cluster(rest)
        if len(second) >= 3:
            res.secondary_scales.append(round(PT_PER_MM_PAPER / median(second), 1))
    elif ratios:
        res.notes.append("dimension votes do not agree with each other")
    res.chains = _chains(res)
    return res


def _chains(res: DimensionResult) -> list[Chain]:
    groups: dict[tuple[str, int], list[Dim]] = {}
    for d in res.dims:
        if not d.consistent:
            continue
        groups.setdefault((d.axis, round(d.pos)), []).append(d)
    # merge groups whose pos differ by one rounding step
    chains: list[Chain] = []
    for (axis, _), dims in sorted(groups.items()):
        dims.sort(key=lambda d: d.lo)
        merged = False
        for c in chains:
            if c.axis == axis and abs(c.pos - dims[0].pos) <= 1.0:
                c.dims.extend(dims)
                c.dims.sort(key=lambda d: d.lo)
                merged = True
                break
        if not merged:
            chains.append(Chain(axis, dims[0].pos, dims, []))
    for c in chains:
        ticks = sorted({round(t, 1) for d in c.dims for t in (d.lo, d.hi)})
        c.ticks = ticks
    # an overall dimension is one whose interval spans another chain's full extent
    for c in chains:
        if len(c.dims) < 2 or res.ratio_pt_per_mm is None:
            continue
        lo, hi = c.ticks[0], c.ticks[-1]
        for d in res.dims:
            if d.consistent and d.axis == c.axis and abs(d.lo - lo) <= 1.5 and abs(d.hi - hi) <= 1.5 and d not in c.dims:
                c.overall = d
                labelled = sum(x.value_mm for x in c.dims)
                covered = sum(x.span_pt for x in c.dims)
                unlabelled_mm = max(0.0, (hi - lo) - covered) / res.ratio_pt_per_mm
                total = labelled + unlabelled_mm
                c.sum_mismatch_pct = abs(total - d.value_mm) / d.value_mm * 100
                break
    return chains


def dims_near(res: DimensionResult, bbox: tuple[float, float, float, float], margin: float) -> list[Dim]:
    """Consistent dimensions whose text lies within `margin` points of a box."""
    x0, y0, x1, y1 = bbox
    out = []
    for d in res.dims:
        if not d.consistent:
            continue
        cx, cy = d.centre
        if x0 - margin <= cx <= x1 + margin and y0 - margin <= cy <= y1 + margin:
            out.append(d)
    return out
