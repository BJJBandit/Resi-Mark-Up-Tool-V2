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
    if not worst:
        return []
    items = ", ".join(f"{t} (up to {g:.0f} mm)" for t, g in sorted(worst.items()))
    return [f"{title}: piers drawn further apart than the office {PIER_MAX_MM:.0f} mm spacing along {items} - "
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
