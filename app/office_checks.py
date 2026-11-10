"""Checks that a sheet follows the office practice in office_rules.md. The rules are
instructions to Claude; these name the places where a sheet departs from them, as
warnings for the engineer (nothing is changed)."""

from __future__ import annotations

import os
import re

import pymupdf

from .schemas import MarkupSheet

PIER_MAX_MM = float(os.environ.get("OFFICE_PIER_SPACING_MM", "3000"))
_ALONG = ("B", "SF", "EB", "IB")  # members piers sit under


def _prefix(tag: str) -> str:
    m = re.match(r"\s*([A-Za-z]+)", tag or "")
    return m.group(1).upper() if m else ""


def pier_spacing(sheet: MarkupSheet, page: pymupdf.Page, scale: int | None, title: str) -> list[str]:
    """Piers further apart than the office spacing along a bearer / footing line."""
    if not scale:
        return []
    w, h = page.rect.width, page.rect.height
    per_mm = 72 / 25.4 / scale
    piers = [pymupdf.Point(e.start.x * w, e.start.y * h) for e in sheet.elements if e.kind in ("pier", "column")]
    worst: dict[str, float] = {}
    for e in sheet.elements:
        if e.kind != "member" or _prefix(e.tag) not in _ALONG:
            continue
        a, b = pymupdf.Point(e.start.x * w, e.start.y * h), pymupdf.Point(e.end.x * w, e.end.y * h)
        d = b - a
        length = abs(d)
        if length < 20:
            continue
        u = d / length
        on = sorted(t for p in piers
                    if -6 <= (t := (p - a).x * u.x + (p - a).y * u.y) <= length + 6
                    and abs((p - a).x * -u.y + (p - a).y * u.x) <= 6)
        if len(on) < 2:
            continue  # no row of piers drawn on this line to measure
        gap = max(q - p for p, q in zip(on, on[1:])) / per_mm
        if gap > PIER_MAX_MM * 1.15:
            tag = e.tag.strip().upper()
            worst[tag] = max(worst.get(tag, 0.0), gap)
    out = []
    # Piers scheduled but (almost) none drawn along a long perimeter footing.
    footing = [e for e in sheet.elements if e.kind == "member" and _prefix(e.tag) in ("SF", "EB")]
    run_mm = sum(abs(pymupdf.Point(e.end.x * w, e.end.y * h) - pymupdf.Point(e.start.x * w, e.start.y * h))
                 for e in footing) / per_mm
    on_footing = 0
    for p in piers:
        for e in footing:
            a, b = pymupdf.Point(e.start.x * w, e.start.y * h), pymupdf.Point(e.end.x * w, e.end.y * h)
            d = b - a
            if abs(d) < 1:
                continue
            t = max(0.0, min(1.0, ((p - a).x * d.x + (p - a).y * d.y) / (abs(d) ** 2)))
            if abs(p - (a + d * t)) <= 6:
                on_footing += 1
                break
    scheduled = any(_prefix(s.tag) == "P" for s in sheet.schedule)
    expected = run_mm / PIER_MAX_MM
    if scheduled and footing and expected >= 4 and on_footing < 0.5 * expected:
        out.append(f"{title}: only {on_footing} pier(s) drawn on {run_mm / 1000:.0f} m of perimeter footing - office "
                   f"practice is piers at corners, steps and max {PIER_MAX_MM:.0f} centres unless the footing is "
                   "founded on the bearing stratum (then say so); check")
    if not worst:
        return out
    items = ", ".join(f"{t} (up to {g:.0f} mm)" for t, g in sorted(worst.items()))
    return out + [f"{title}: piers drawn further apart than the office {PIER_MAX_MM:.0f} mm spacing along {items} - "
                  "check a pier isn't missing"]


_STEEL_DECK = re.compile(r"STEEL|FLAME.?SHIELD|\bRHS\b|\bPFC\b|\bUB\b|\bC\d{5}\b|PURLIN|ALUMIN", re.I)


def footing_practice(sheet: MarkupSheet, title: str) -> list[str]:
    """Perimeter footings with less than 4/L11TM top and bottom, and concrete below N25."""
    out, weak, low = [], [], []
    for s in sheet.schedule:
        text, p = s.text.upper(), _prefix(s.tag)
        if p in ("SF", "EB"):
            bars = re.search(r"(\d+)\s*/\s*L(\d{2})\s*TM?", text)
            both = re.search(r"TOP\s*(?:AND|&)\s*BOTTOM|T\s*&\s*B|\bT\s*AND\s*B\b", text)
            if not bars or int(bars.group(1)) < 4 or int(bars.group(2)) < 11 or not both:
                weak.append(s.tag)
        g = re.search(r"\bN(\d{2})\b", text)
        if g and int(g.group(1)) < 25:
            low.append(f"{s.tag} (N{g.group(1)})")
    if weak:
        out.append(f"{title}: perimeter footing(s) without 4/L11TM top and bottom ({', '.join(weak)}) - office "
                   "practice is 450 x min 450 with 4/L11TM top and bottom")
    if low:
        out.append(f"{title}: concrete below N25 ({', '.join(low)}) - office practice is N25 (25 MPa)")
    return out


def schedule_practice(sheet: MarkupSheet, title: str) -> list[str]:
    """Schedule items that depart from office practice: steel deck framing, lintels that
    aren't single F17 / LVL members, more than one deck joist type, a single bracing type."""
    out = []
    steel_deck, bad_lintels, deck_joists = [], [], []
    for s in sheet.schedule:
        text, p = s.text.upper(), _prefix(s.tag)
        if "DECK" in text and p in ("B", "J", "LD", "L") and _STEEL_DECK.search(text):
            steel_deck.append(s.tag)
        if p == "L" and "LINTEL" in text and "DECK" not in text:
            if re.search(r"\b\d/\d{2,3}\s*X", text) or not re.search(r"F17|LVL", text):
                bad_lintels.append(s.tag)
        if p == "J" and "DECK" in text:
            deck_joists.append(s.tag)
    if steel_deck:
        out.append(f"{title}: deck framing in steel ({', '.join(steel_deck)}) - office practice is timber decks, "
                   "including ledgers, unless the engineer directs otherwise")
    if bad_lintels:
        out.append(f"{title}: lintel(s) not a single F17 / LVL member ({', '.join(bad_lintels)}) - office practice "
                   "is single F17 / LVL lintels; confirm")
    if len(deck_joists) > 1:
        out.append(f"{title}: {len(deck_joists)} deck joist types ({', '.join(deck_joists)}) - office practice is one "
                   "size and tag, sized for the longest span")
    kinds = {re.match(r"\s*([a-z])", e.tag.lower()).group(1) for e in sheet.elements
             if e.kind == "bracing" and re.match(r"\s*[a-z]", e.tag.lower())}
    n_bracing = sum(e.kind == "bracing" for e in sheet.elements)
    if n_bracing >= 6 and len(kinds) == 1:
        out.append(f"{title}: all {n_bracing} bracing panels are type {kinds.pop()} - office practice is a mix of "
                   "h(b) ply on external walls, d strap on internal walls and g short panels; confirm")
    return out


_EXTERNAL = re.compile(r"PORCH|PAVING|PATIO|ALFRESCO|TERRACE|ENTRY SLAB|PATH", re.I)


def external_slabs(sheet: MarkupSheet, title: str) -> list[str]:
    """External slabs (porch / paving / patio) drawn wrapped in edge beams or strip footing,
    or scheduled with one - office practice is a separate slab, isolated from the building
    with an expansion joint, no edge beam unless a load needs one. Carports keep theirs."""
    specs = {s.tag.strip().upper(): s.text.upper() for s in sheet.schedule}
    flagged = []
    for s in sheet.schedule:
        if _prefix(s.tag) == "S" and _EXTERNAL.search(s.text) and re.search(r"EDGE BEAM|MONOLITHIC|\bEB\d", s.text, re.I):
            flagged.append(s.tag.strip().upper())
        if _prefix(s.tag) in ("EB", "SF") and _EXTERNAL.search(s.text):
            flagged.append(s.tag.strip().upper())
    beams = [e for e in sheet.elements if e.kind == "member" and _prefix(e.tag) in ("EB", "SF")]
    for e in sheet.elements:
        tag = e.tag.strip().upper()
        if e.kind != "member" or _prefix(tag) != "S" or not _EXTERNAL.search(f"{e.text} {specs.get(tag, '')}"):
            continue
        x0, x1 = sorted((e.start.x, e.end.x))
        y0, y1 = sorted((e.start.y, e.end.y))
        tol = 0.006

        def within(p):
            return x0 - tol <= p.x <= x1 + tol and y0 - tol <= p.y <= y1 + tol

        # Two or more footing runs round the slab: wrapped (one shared side is the house line).
        if sum(within(b.start) and within(b.end) for b in beams) >= 2 and tag not in flagged:
            flagged.append(tag)
    if not flagged:
        return []
    return [f"{title}: external slab drawn or scheduled with an edge beam / footing ({', '.join(flagged)}) - "
            "office practice is a separate slab isolated from the building with a 10 Abelflex expansion joint; confirm"]
