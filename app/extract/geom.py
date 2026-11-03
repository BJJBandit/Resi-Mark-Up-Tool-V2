"""Small 2-D primitives shared by the extraction steps.

Everything here is in *page points with y up* (PDF user space). Axis-aligned
segments are the workhorse: a plan drawing is overwhelmingly horizontal and
vertical lines, so each segment stores which axis it lies on, its position across
that axis (`pos`) and its extent along it (`lo`..`hi`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable, Literal

PT_PER_MM_PAPER = 72.0 / 25.4  # points per millimetre of paper

Axis = Literal["h", "v"]


def pts_per_mm(scale_denominator: float) -> float:
    """Page points per real-world millimetre at a drawing scale (100 for 1:100)."""
    return PT_PER_MM_PAPER / scale_denominator


@dataclass(frozen=True)
class Seg:
    """A straight segment. `kind` says where it came from: a stroked path, the edge
    of a filled polygon, or a curve's chord (kept only for door-swing evidence)."""

    x0: float
    y0: float
    x1: float
    y1: float
    width: float = 0.0
    kind: Literal["stroke", "fill", "curve"] = "stroke"
    dashed: bool = False

    @property
    def length(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)

    @property
    def axis(self) -> Axis | None:
        if self.y0 == self.y1 and self.x0 != self.x1:
            return "h"
        if self.x0 == self.x1 and self.y0 != self.y1:
            return "v"
        return None

    @property
    def pos(self) -> float:
        """Coordinate across the axis (y for a horizontal segment, x for a vertical)."""
        return self.y0 if self.axis == "h" else self.x0

    @property
    def lo(self) -> float:
        return min(self.x0, self.x1) if self.axis == "h" else min(self.y0, self.y1)

    @property
    def hi(self) -> float:
        return max(self.x0, self.x1) if self.axis == "h" else max(self.y0, self.y1)

    @property
    def mid(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def bbox(self) -> tuple[float, float, float, float]:
        return (min(self.x0, self.x1), min(self.y0, self.y1), max(self.x0, self.x1), max(self.y0, self.y1))


@dataclass(frozen=True)
class Bar:
    """An axis-aligned wall bar: centre-line at `pos`, running `lo`..`hi` along `axis`,
    `thickness` points thick. Produced either directly from a filled rectangle or from
    a pair of parallel lines."""

    axis: Axis
    pos: float
    lo: float
    hi: float
    thickness: float
    source: Literal["fill", "pair"] = "pair"

    @property
    def length(self) -> float:
        return self.hi - self.lo

    def point(self, along: float) -> tuple[float, float]:
        return (along, self.pos) if self.axis == "h" else (self.pos, along)

    @property
    def start(self) -> tuple[float, float]:
        return self.point(self.lo)

    @property
    def end(self) -> tuple[float, float]:
        return self.point(self.hi)

    def footprint(self, end_extension: float = 0.0) -> tuple[float, float, float, float]:
        """Axis-aligned box covered by the bar, optionally extended at both ends."""
        half = self.thickness / 2
        lo, hi = self.lo - end_extension, self.hi + end_extension
        if self.axis == "h":
            return (lo, self.pos - half, hi, self.pos + half)
        return (self.pos - half, lo, self.pos + half, hi)


def snap_to_axis(seg: Seg, max_angle_deg: float = 2.0, max_offset: float = 0.6) -> Seg:
    """Make a nearly horizontal / vertical segment exactly so. Diagonals are untouched."""
    dx, dy = seg.x1 - seg.x0, seg.y1 - seg.y0
    if dx == 0 or dy == 0:
        return seg
    tan_max = math.tan(math.radians(max_angle_deg))
    if abs(dy) <= max(max_offset, abs(dx) * tan_max) and abs(dy) < abs(dx):
        y = (seg.y0 + seg.y1) / 2
        return replace(seg, y0=y, y1=y)
    if abs(dx) <= max(max_offset, abs(dy) * tan_max) and abs(dx) < abs(dy):
        x = (seg.x0 + seg.x1) / 2
        return replace(seg, x0=x, x1=x)
    return seg


def _merge_key(seg: Seg) -> tuple:
    return (seg.axis, seg.kind, round(seg.width, 1), seg.dashed)


def merge_collinear(segs: Iterable[Seg], pos_tol: float = 0.35, gap_tol: float = 0.35) -> list[Seg]:
    """Join axis-aligned segments that lie on the same line (within `pos_tol`) and
    overlap or touch (within `gap_tol`). Only segments of the same kind and pen width
    are joined, so a window line drawn with a lighter pen never bridges a wall gap.
    Diagonal segments pass through unchanged."""
    out: list[Seg] = []
    groups: dict[tuple, list[Seg]] = {}
    for s in segs:
        if s.axis is None:
            out.append(s)
        else:
            groups.setdefault(_merge_key(s), []).append(s)
    for key, group in groups.items():
        group.sort(key=lambda s: s.pos)
        # cluster by position across the axis
        clusters: list[list[Seg]] = []
        for s in group:
            if clusters and s.pos - clusters[-1][-1].pos <= pos_tol:
                clusters[-1].append(s)
            else:
                clusters.append([s])
        for cluster in clusters:
            cluster.sort(key=lambda s: s.lo)
            cur_lo, cur_hi = cluster[0].lo, cluster[0].hi
            weighted_pos, weight = cluster[0].pos * max(cluster[0].length, 1e-6), max(cluster[0].length, 1e-6)
            for s in cluster[1:]:
                if s.lo <= cur_hi + gap_tol:
                    cur_hi = max(cur_hi, s.hi)
                    weighted_pos += s.pos * max(s.length, 1e-6)
                    weight += max(s.length, 1e-6)
                else:
                    out.append(_make(key, weighted_pos / weight, cur_lo, cur_hi, cluster[0]))
                    cur_lo, cur_hi = s.lo, s.hi
                    weighted_pos, weight = s.pos * max(s.length, 1e-6), max(s.length, 1e-6)
            out.append(_make(key, weighted_pos / weight, cur_lo, cur_hi, cluster[0]))
    return out


def _make(key: tuple, pos: float, lo: float, hi: float, like: Seg) -> Seg:
    axis = key[0]
    if axis == "h":
        return replace(like, x0=lo, y0=pos, x1=hi, y1=pos)
    return replace(like, x0=pos, y0=lo, x1=pos, y1=hi)


def overlap(lo0: float, hi0: float, lo1: float, hi1: float) -> float:
    """Length of the common part of two intervals (negative = gap between them)."""
    return min(hi0, hi1) - max(lo0, lo1)


def rect_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float], slack: float = 0.0) -> bool:
    return a[0] <= b[2] + slack and b[0] <= a[2] + slack and a[1] <= b[3] + slack and b[1] <= a[3] + slack


def rect_centre(r: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)


def rect_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)
