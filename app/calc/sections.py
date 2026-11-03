"""Steel section catalogue loader.

Reads ``app/data/standards/steel_sections/{ub,pfc,shs}.yaml`` into :class:`Section`
objects.  Units follow the YAML files: mm, mm², mm⁴, mm³, mm⁶, kg/m, MPa.

Every section carries ``verified`` and ``source``.  The engine quotes both in its trace;
``verified: false`` makes it emit "section properties unverified".
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

CATALOGUE_DIR = Path(__file__).resolve().parents[1] / "data" / "standards" / "steel_sections"

Family = Literal["UB", "PFC", "SHS"]


class Section(BaseModel):
    """One catalogue row. Optional fields are family-specific (see the YAML headers)."""

    family: Family
    designation: str
    mass_kg_m: float
    d: float                      # overall depth
    bf: float                     # overall flange width (UB/PFC) or B (SHS)
    tf: float                     # flange thickness (UB/PFC) or wall t (SHS)
    tw: float                     # web thickness (UB/PFC) or wall t (SHS)
    r1: float = 0.0               # root radius (UB/PFC); outside corner radius (SHS)
    xL: float | None = None       # PFC: centroid from back of web
    Ag: float
    Ix: float
    Iy: float
    Zx: float
    Zy: float
    Sx: float
    Sy: float
    rx: float
    ry: float
    J: float
    Iw: float = 0.0
    fy_flange: float              # MPa, AS 4100 Table 2.1 (by element thickness)
    fy_web: float
    residual_stress: Literal["HR", "CF", "LW", "HW", "SR"] = "HR"   # AS 4100 Tables 5.2 / 6.2.4
    verified: bool = False
    source: str = ""
    standard: str = ""
    grade: str = ""

    @property
    def fy(self) -> float:
        """Design yield used for bending/compression: the lower of flange and web (AS 4100
        cl 5.2.1 note: use the flange fy for Ms; taking the minimum is conservative)."""
        return min(self.fy_flange, self.fy_web)

    @property
    def is_hollow(self) -> bool:
        return self.family == "SHS"

    @property
    def E_label(self) -> str:
        return f"{self.designation} ({self.grade} {self.standard})"


def _row_to_section(row: dict, meta: dict) -> Section:
    fam = meta["family"]
    common = dict(
        family=fam, designation=row["designation"], mass_kg_m=row["mass_kg_m"],
        Ag=row["Ag"], Ix=row["Ix"], Iy=row["Iy"], Zx=row["Zx"], Zy=row["Zy"],
        Sx=row["Sx"], Sy=row["Sy"], rx=row["rx"], ry=row["ry"], J=row["J"],
        Iw=row.get("Iw", 0.0) or 0.0, verified=bool(row.get("verified", False)),
        source=row.get("source", ""), standard=str(meta.get("standard", "")),
        grade=str(meta.get("grade", "")), residual_stress=meta.get("residual_stress_category", "HR"),
    )
    if fam == "SHS":
        return Section(d=row["d"], bf=row["b"], tf=row["t"], tw=row["t"], r1=row.get("r_ext", 2.5 * row["t"]),
                       fy_flange=row["fy"], fy_web=row["fy"], **common)
    return Section(d=row["d"], bf=row["bf"], tf=row["tf"], tw=row["tw"], r1=row.get("r1", 0.0),
                   xL=row.get("xL"), fy_flange=row["fy_flange"], fy_web=row["fy_web"], **common)


def load_family(family: Family, catalogue_dir: Path | None = None) -> list[Section]:
    """Load one family, sorted lightest → heaviest (the order ``design_beam`` walks)."""
    path = Path(catalogue_dir or CATALOGUE_DIR) / f"{family.lower()}.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    meta = {k: v for k, v in data.items() if k != "sections"}
    meta.setdefault("family", family)
    rows = [_row_to_section(r, meta) for r in data.get("sections", [])]
    return sorted(rows, key=lambda s: (s.mass_kg_m, s.d))


@lru_cache(maxsize=None)
def load_catalogue(catalogue_dir: str | None = None) -> dict[Family, list[Section]]:
    d = Path(catalogue_dir) if catalogue_dir else CATALOGUE_DIR
    return {fam: load_family(fam, d) for fam in ("UB", "PFC", "SHS")}


def get_section(designation: str, catalogue_dir: str | None = None) -> Section:
    """Look up by designation, tolerant of spacing/case (``"250 PFC"``, ``"250PFC"``, ``"89x89x5 SHS"``)."""
    key = _norm(designation)
    for fam in load_catalogue(catalogue_dir).values():
        for s in fam:
            if _norm(s.designation) == key:
                return s
    # second pass: allow "89x5 SHS" / "89x89x5.0 SHS" style equivalence
    for fam in load_catalogue(catalogue_dir).values():
        for s in fam:
            if _norm_shs(s.designation) == _norm_shs(designation):
                return s
    raise KeyError(f"section {designation!r} not in catalogue")


def _norm(s: str) -> str:
    return "".join(s.lower().split()).replace("×", "x")


def _norm_shs(s: str) -> str:
    s = _norm(s).replace("shs", "")
    parts = s.split("x")
    try:
        nums = [float(p) for p in parts if p]
    except ValueError:
        return s
    if len(nums) == 3:
        nums = [nums[0], nums[2]]
    return "x".join(f"{n:g}" for n in nums)
