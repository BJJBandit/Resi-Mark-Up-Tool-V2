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
