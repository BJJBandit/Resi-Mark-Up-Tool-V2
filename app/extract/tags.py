"""Opening tags: the architect's codes next to windows and doors.

Two conventions are recognised:

* Maveric style `A1524`, `F0612`, `SD2118`: letter(s) for the type (A awning,
  F fixed, SD sliding door, ...) then height x width in centimetres.
* Another Perspective style `15-24AW`, `21-27SD (DG)`, `21-27 Roller Door`:
  height-width in centimetres then the type; `W05*` is only an id.
* A bare `820`, `920`, `820 CSD` is a door leaf width; `5200w ROLLER DOOR` a garage door.

Anything else is left unparsed and reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OpeningKind = str  # mirrors schema.OpeningKind

_TYPE_KIND: dict[str, OpeningKind] = {
    "A": "window", "AW": "window", "F": "window", "FW": "window", "S": "window", "SW": "window",
    "DH": "window", "BF": "window", "CS": "window", "L": "window", "H": "window",
    "SD": "sliding_door", "SSD": "sliding_door", "SSO": "sliding_door", "SLD": "sliding_door",
    "D": "door", "FD": "door", "ED": "door", "CSD": "door",
}

_MAVERIC = re.compile(r"^(SD|SSD|CS|BF|DH|AW|FW|SW|A|F|S|D|L|H)(\d{2})(\d{2})$")
_AP = re.compile(r"^(\d{2})-(\d{2,3})\s*(SSD|SSO|SLD|AW|FW|SW|DH|BF|CS|SD|FD|ED)\b")
_AP_GARAGE = re.compile(r"^(\d{2})-(\d{2,3})\s*(ROLLER|PANEL LIFT|TILT|SECTIONAL)", re.I)
_DOOR = re.compile(r"^(\d{3,4})\s*(CSD|CAV|SLD)?$", re.I)
_GARAGE = re.compile(r"^(\d{4})\s*W?\s*(ROLLER|PANEL LIFT|TILT|SECTIONAL|GARAGE)", re.I)
_WINDOW_ID = re.compile(r"^W\d{1,3}\*?$")
_DOOR_WIDTHS = (520, 620, 720, 770, 820, 870, 920, 1020, 1200)


@dataclass(frozen=True)
class Tag:
    text: str
    kind: OpeningKind
    width_mm: float | None
    height_mm: float | None
    is_id_only: bool = False


_NOT_A_TAG = re.compile(r"^(CL|RL|FFL|FCL|FSL|AHD)\s*\d")


def parse_tag(text: str) -> Tag | None:
    t = text.strip()
    up = t.upper()
    if _NOT_A_TAG.match(up):
        return None  # ceiling heights and levels
    m = _MAVERIC.match(up)
    if m:
        kind = _TYPE_KIND.get(m.group(1), "unknown")
        return Tag(t, kind, int(m.group(3)) * 100.0, int(m.group(2)) * 100.0)
    m = _AP.match(up)
    if m:
        kind = _TYPE_KIND.get(m.group(3), "unknown")
        return Tag(t, kind, int(m.group(2)) * 100.0, int(m.group(1)) * 100.0)
    m = _AP_GARAGE.match(up)
    if m:
        return Tag(t, "garage_door", int(m.group(2)) * 100.0, int(m.group(1)) * 100.0)
    m = _GARAGE.match(up)
    if m:
        return Tag(t, "garage_door", float(m.group(1)), None)
    m = _DOOR.match(up)
    if m:
        w = int(m.group(1))
        if 520 <= w <= 1250 and (m.group(2) or w in _DOOR_WIDTHS):
            return Tag(t, "door", float(w), None)
        return None
    if _WINDOW_ID.match(up):
        return Tag(t, "window", None, None, is_id_only=True)
    return None


def looks_like_tag(text: str) -> bool:
    """True for strings that are probably opening tags even when unparsed, so they can
    be reported as unmatched (e.g. a code style we have not seen)."""
    up = text.strip().upper()
    if _NOT_A_TAG.match(up):
        return False
    return bool(parse_tag(up)) or bool(re.match(r"^[A-Z]{1,3}\d{3,4}[A-Z]?$", up))
