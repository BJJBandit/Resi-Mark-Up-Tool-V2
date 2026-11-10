"""British / Australian English for everything written on the mark-ups. Claude is told to
write it; this catches the common American spellings that slip through (whole words only,
keeping the case: CENTER -> CENTRE, Center -> Centre)."""

from __future__ import annotations

import re

US_TO_AU = {
    "center": "centre", "centers": "centres", "centered": "centred", "centering": "centring",
    "meter": "metre", "meters": "metres", "millimeter": "millimetre", "millimeters": "millimetres",
    "centimeter": "centimetre", "centimeters": "centimetres", "liter": "litre", "liters": "litres",
    "color": "colour", "colors": "colours", "colored": "coloured", "fiber": "fibre", "fibers": "fibres",
    "aluminum": "aluminium", "gray": "grey", "mold": "mould", "molds": "moulds", "molded": "moulded",
    "galvanized": "galvanised", "galvanize": "galvanise", "galvanizing": "galvanising",
    "stabilize": "stabilise", "stabilized": "stabilised", "stabilizing": "stabilising",
    "utilize": "utilise", "utilized": "utilised", "minimize": "minimise", "minimized": "minimised",
    "maximize": "maximise", "maximized": "maximised", "specialized": "specialised",
    "organization": "organisation", "analyze": "analyse", "analyzed": "analysed",
    "labeled": "labelled", "labeling": "labelling", "leveled": "levelled", "leveling": "levelling",
    "modeled": "modelled", "modeling": "modelling", "traveled": "travelled", "canceled": "cancelled",
    "defense": "defence", "gage": "gauge", "gages": "gauges", "curb": "kerb", "curbs": "kerbs",
    "plow": "plough", "sulfate": "sulphate", "sulfates": "sulphates",
    "favor": "favour", "behavior": "behaviour", "neighbor": "neighbour", "neighboring": "neighbouring",
    "endeavor": "endeavour", "harbor": "harbour", "vapor": "vapour", "armor": "armour",
    "catalog": "catalogue", "dialog": "dialogue", "practicing": "practising",
}
_RX = re.compile(r"\b(" + "|".join(sorted(US_TO_AU, key=len, reverse=True)) + r")\b", re.I)


def _keep_case(src: str, dst: str) -> str:
    if src.isupper():
        return dst.upper()
    if src[:1].isupper():
        return dst[:1].upper() + dst[1:]
    return dst


def to_au(text: str) -> str:
    if not text:
        return text
    return _RX.sub(lambda m: _keep_case(m.group(0), US_TO_AU[m.group(0).lower()]), text)


# Units keep their SI case in the upper-case notes (and anywhere else on a sheet):
# 450 MM -> 450 mm, 27 KN -> 27 kN, 0.8 KPA -> 0.8 kPa, 36.3 M2 -> 36.3 m². Only after a
# number, so bolt sizes (M12), concrete grades (N25) and bars (L11, N12) are left alone.
_UNITS = [("KN/M2", "kN/m²"), ("KN/M²", "kN/m²"), ("KN/M", "kN/m"), ("KNM", "kNm"), ("KN", "kN"),
          ("KPA", "kPa"), ("MPA", "MPa"), ("MM2", "mm²"), ("MM²", "mm²"), ("MM3", "mm³"), ("MM³", "mm³"),
          ("MM4", "mm⁴"), ("MM⁴", "mm⁴"), ("MM", "mm"), ("M2", "m²"), ("M²", "m²"),
          ("M3", "m³"), ("M³", "m³"), ("M/S", "m/s"), ("KG", "kg"), ("M", "m")]
_UNIT_RX = re.compile(
    r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?[⁰¹²³⁴⁵⁶⁷⁸⁹]*)(\s?)(" + "|".join(re.escape(u) for u, _ in _UNITS) + r")(?![A-Za-z0-9²³⁴/])",
    re.I)
_UNIT_MAP = {u.upper(): v for u, v in _UNITS}
_DIM_X = re.compile(r"(\d)(W?)(\s?)X(\s?)(?=\d)")  # 90X35 -> 90x35, 450W X 450 -> 450W x 450
# A multiply sign after a number or unit ('2.40 m X 1.50', '4.45 X TAN', '450W X MIN'): x.
_TIMES = re.compile(r"(?<=[\d²³⁴⁶a-z)])(\s)X(?=\s)|(?<=\dW)(\s)X(?=\s)")
_UNKNOWN_REF = re.compile(r"(\d)\.\?(?!\w)")  # 'CLAUSE 2.6.?' -> 'CLAUSE 2.6'
_UNITS_ALONE_TIMES = re.compile(r"\b(ELEVATION|LENGTH|AREA|LOAD) X (?=[A-Z(])")


_ALONE = re.compile(r"(?<![A-Za-z0-9])(KN/M2|KN/M|KNM|KN|KPA|MPA)(?![A-Za-z0-9²])")  # never anything else


def units(text: str) -> str:
    if not text:
        return text
    text = _ALONE.sub(lambda m: _UNIT_MAP[m.group(1)], text)
    text = _UNIT_RX.sub(lambda m: m.group(1) + m.group(2) + _UNIT_MAP.get(m.group(3).upper(), m.group(3)), text)
    text = _DIM_X.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + "x" + m.group(4), text)
    text = _TIMES.sub(lambda m: (m.group(1) or m.group(2)) + "x", text)
    text = _UNITS_ALONE_TIMES.sub(r"\1 x ", text)
    return _UNKNOWN_REF.sub(r"\1", text)


# Notes never say a value was taken 'from memory' (or that a table wasn't supplied): they
# cite the standard, and a value to be checked is 'ENGINEER TO CONFIRM'.
_MEMORY = re.compile(
    r"(?:\b(?:TABLES?|STANDARDS?|DOCUMENTS?)\s+(?:WERE\s+|WAS\s+)?NOT\s+SUPPLIED\s*[,;:-]?\s*)?"
    r"(?:\b(?:VALUES?|CAPACIT(?:Y|IES)|PRESSURES?|FIGURES?)\s+)?(?:(?:TAKEN|QUOTED|ADOPTED|READ|USED)\s+)?"
    r"FROM\s+MEMORY\b(?:\s*\([^)]*\))?(?:\s*[,;:-]?\s*(?:ENGINEER\s+TO\s+)?CONFIRM(?:ED)?\b)?", re.I)
_NOT_SUPPLIED = re.compile(r"\b(?:TABLES?|STANDARDS?)\s+(?:WERE\s+|WAS\s+)?NOT\s+SUPPLIED\b"
                           r"(?:\s*[,;:-]?\s*(?:ENGINEER\s+TO\s+)?CONFIRM(?:ED)?\b)?", re.I)
_DOUBLE = re.compile(r"(ENGINEER TO CONFIRM)(?:\s*[,;:.-]?\s*(?:ENGINEER\s+TO\s+)?CONFIRM(?:ED)?\b)+", re.I)


def no_memory(text: str) -> str:
    if not text or not re.search(r"MEMORY|NOT\s+SUPPLIED", text, re.I):
        return text
    rep = "ENGINEER TO CONFIRM" if text.upper() == text else "engineer to confirm"
    text = _MEMORY.sub(rep, text)
    text = _NOT_SUPPLIED.sub(rep, text)
    text = _DOUBLE.sub(r"\1", text)
    return re.sub(r"\s{2,}", " ", text)


def tidy(text: str) -> str:
    """Everything written on a sheet: Australian spelling, no 'from memory', units in SI case."""
    return units(no_memory(to_au(text)))
