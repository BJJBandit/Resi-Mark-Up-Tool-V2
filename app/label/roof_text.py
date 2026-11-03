"""Deterministic roof facts from drawing text - runs before any LLM call.

Architects write the roof on the elevations and sections: "Colorbond roof, fascia &
gutters @ 22.5° pitch", "Timber engineered trusses at 900 centres", "450 wide eaves".
These are regex-grade facts; only the roof *form* (hip / gable / skillion) needs a look
at the drawing, and even that is often in the text.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..model import Provenance, Roof

_PITCH = re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:°|º|deg(?:rees)?)\s*(?:roof\s*)?pitch|pitch\s*(?:of\s*|@\s*|at\s*)?(\d{1,2}(?:\.\d)?)\s*(?:°|º|deg)", re.I)
_SHEET = re.compile(r"colorbond|custom\s*orb|trimdek|klip-?lok|corrugated|metal\s+roof|sheet\s+roof|zincalume", re.I)
_TILE = re.compile(r"(?:concrete|terracotta|roof)\s+tiles?|tiled\s+roof", re.I)
_TRUSS = re.compile(r"\btruss(?:es)?\b", re.I)
_RAFTER = re.compile(r"\brafters?\b|skillion|cathedral|raked\s+ceiling", re.I)
_EAVES = re.compile(r"(\d{3,4})\s*(?:mm)?\s*(?:wide\s+)?eaves|eaves?\s*(?:o'?hang|overhang)?\D{0,12}(\d{3,4})", re.I)
_FORM = {
    "skillion": re.compile(r"skillion|mono-?pitch", re.I),
    "gable": re.compile(r"\bgable", re.I),
    "hip": re.compile(r"\bhip(?:ped)?\b", re.I),
    "flat": re.compile(r"\bflat\s+roof", re.I),
}


@dataclass
class RoofText:
    pitches_deg: list[float] = field(default_factory=list)
    cladding: str = "unknown"
    construction: str = "unknown"
    eaves_mm: int | None = None
    form_hints: Counter = field(default_factory=Counter)
    pages: list[int] = field(default_factory=list)

    @property
    def pitch_deg(self) -> float | None:
        """Most common stated pitch (a set often repeats the main pitch on every elevation
        and mentions a secondary one once, e.g. 22.5° main / 15° garage)."""
        if not self.pitches_deg:
            return None
        return Counter(self.pitches_deg).most_common(1)[0][0]


def scan_roof_text(page_texts: dict[int, str]) -> RoofText:
    """page_texts: {page_number: full text}. Only elevation/section/roof-plan pages need
    be passed, but passing everything is harmless."""
    rt = RoofText()
    for page, text in sorted(page_texts.items()):
        hit = False
        for m in _PITCH.finditer(text):
            val = m.group(1) or m.group(2)
            if val:
                rt.pitches_deg.append(float(val))
                hit = True
        if _SHEET.search(text):
            rt.cladding = "sheet"
            hit = True
        elif _TILE.search(text) and rt.cladding == "unknown":
            rt.cladding = "tile"
            hit = True
        if _TRUSS.search(text):
            rt.construction = "truss" if rt.construction != "rafter" else "mixed"
            hit = True
        if _RAFTER.search(text):
            rt.construction = "rafter" if rt.construction != "truss" else "mixed"
            hit = True
        m = _EAVES.search(text)
        if m and rt.eaves_mm is None:
            val = int(m.group(1) or m.group(2))
            if 100 <= val <= 1500:
                rt.eaves_mm = val
                hit = True
        for form, rx in _FORM.items():
            n = len(rx.findall(text))
            if n:
                rt.form_hints[form] += n
                hit = True
        if hit:
            rt.pages.append(page)
    return rt


def apply_roof_text(roof: Roof, rt: RoofText) -> list[str]:
    """Fill a Roof from text facts, returning a list of what was set (for the trace)."""
    set_: list[str] = []
    page = rt.pages[0] if rt.pages else None
    if roof.pitch_deg is None and rt.pitch_deg is not None:
        roof.pitch_deg = rt.pitch_deg
        set_.append(f"pitch {rt.pitch_deg}° from drawing text")
    if roof.cladding == "unknown" and rt.cladding != "unknown":
        roof.cladding = rt.cladding  # type: ignore[assignment]
        set_.append(f"cladding '{rt.cladding}' from drawing text")
    if roof.construction == "unknown" and rt.construction != "unknown":
        roof.construction = rt.construction  # type: ignore[assignment]
        set_.append(f"construction '{rt.construction}' from drawing text")
    if roof.eaves_mm is None and rt.eaves_mm:
        roof.eaves_mm = rt.eaves_mm
        set_.append(f"eaves {rt.eaves_mm} from drawing text")
    if set_:
        roof.provenance = Provenance(source="pdf_text", page=page, confidence=0.9, note="; ".join(set_))
    return set_
