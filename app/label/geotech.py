"""Geotechnical report -> `Geotech` site model. One Claude call on the PDF."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from ..model import Geotech, Provenance, Site
from .llm import UsageTotals, call_structured, pdf_block, text_block

NS = "Use 'not stated' when the report does not give it."


class GeotechExtract(BaseModel):
    """Flat strings on purpose: the API compiles the schema into a grammar; numbers are
    parsed deterministically afterwards so units and ranges are handled in code."""

    model_config = ConfigDict(extra="forbid")

    report_found: bool
    site_classification: str = Field(description="AS 2870 class exactly as written: A, S, M, M-D, H1, H1-D, H2, H2-D, E, E-D or P.")
    ys: str = Field(description=f"Characteristic surface movement with unit, e.g. '30 mm'. {NS}")
    bearing: str = Field(description=f"Allowable bearing pressure with unit and depth if given, e.g. '100 kPa at 500 mm'. {NS}")
    founding_depth: str = Field(description=f"Recommended founding depth with unit. {NS}")
    founding_material: str = Field(description=f"Material to found in/on, e.g. 'stiff clay', 'dolerite bedrock'. {NS}")
    rock_depth: str = Field(description=f"Depth to rock / bedrock with unit, or a range. {NS}")
    fill: str = Field(description=f"Fill: 'none', or depth with unit and type. {NS}")
    groundwater: str = Field(description=NS)
    trees_or_abnormal_moisture: str = Field(description=f"'yes' / 'no' plus a few words. {NS}")
    landslip_or_slope_hazard: str = Field(description=f"'yes' / 'no' plus a few words. {NS}")
    wind_classification: str = Field(description=f"AS 4055 wind class if the report states one. {NS}")
    recommendations: list[str] = Field(description="The report's footing / construction recommendations, one per item, short.")
    concerns: list[str] = Field(description="Anything the engineer must resolve, one per item, short.")


_NUM = re.compile(r"(-?\d+(?:\.\d+)?)")
_SITE = re.compile(r"\b(A|S|M-D|M|H1-D|H1|H2-D|H2|E-D|E|P)\b")


def _mm(text: str) -> float | None:
    """'30 mm' -> 30; '0.5 m' -> 500; '0.2 – 0.85 m' -> 850 (take the deeper bound)."""
    t = text.lower()
    if "not stated" in t:
        return None
    nums = [float(n) for n in _NUM.findall(t)]
    if not nums:
        return None
    val = max(nums)
    if re.search(r"\bm\b|metre", t) and not re.search(r"\bmm\b", t):
        val *= 1000
    return val


def _kpa(text: str) -> float | None:
    t = text.lower()
    if "not stated" in t:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*kpa", t)
    return float(m.group(1)) if m else None


def _yes(text: str) -> bool | None:
    t = text.lower()
    if "not stated" in t:
        return None
    return t.startswith("yes") or (" yes" in t and "no " not in t[:3])


def to_geotech(x: GeotechExtract) -> Geotech:
    g = Geotech(report_provided=x.report_found)
    m = _SITE.search(x.site_classification.upper().replace("CLASS", ""))
    if m:
        g.site_class = m.group(1)  # type: ignore[assignment]
    g.ys_mm = _mm(x.ys)
    g.bearing_kpa = _kpa(x.bearing)
    g.founding_depth_mm = _mm(x.founding_depth)
    g.founding_material = "" if "not stated" in x.founding_material.lower() else x.founding_material
    g.rock_depth_mm = _mm(x.rock_depth)
    fl = x.fill.lower()
    g.fill_present = None if "not stated" in fl else not fl.startswith("none") and "no fill" not in fl
    g.fill_depth_mm = _mm(x.fill) if g.fill_present else None
    g.groundwater = "" if "not stated" in x.groundwater.lower() else x.groundwater
    g.trees_or_abnormal_moisture = _yes(x.trees_or_abnormal_moisture)
    g.landslip_or_slope_hazard = _yes(x.landslip_or_slope_hazard)
    g.recommendations = x.recommendations
    g.concerns = x.concerns
    return g


def parse_geotech(pdf_bytes: bytes, site: Site, totals: UsageTotals) -> list[str]:
    """Fill `site.geotech` (and a stated wind class if the report gives one). Returns review notes."""
    content = [
        pdf_block(pdf_bytes, "Geotechnical report"),
        text_block(
            "Extract the site information for AS 2870 footing design. Quote values as written "
            "(with units); do not convert or infer. If the document is not a geotechnical "
            "report, set report_found=false and leave the rest 'not stated'."
        ),
    ]
    x = call_structured(content, GeotechExtract, "geotech", totals, max_tokens=4000)
    site.geotech = to_geotech(x)
    for name in ("site_class", "ys_mm", "bearing_kpa", "founding_depth_mm", "rock_depth_mm", "fill_present"):
        site.provenance[f"geotech.{name}"] = Provenance(source="geotech", confidence=0.9)
    notes: list[str] = []
    wm = re.search(r"\b([NC][1-6])\b", x.wind_classification.upper())
    if wm and site.wind.stated_class is None:
        site.wind.stated_class = wm.group(1)  # type: ignore[assignment]
        site.wind.stated_source = "geotech"
    if site.geotech.site_class == "Unknown":
        notes.append("Site class not found in the geotech report - enter it in review")
    if site.geotech.site_class == "P":
        notes.append("Class P site: AS 2870 standard designs do not apply - engineered footings")
    if site.geotech.landslip_or_slope_hazard:
        notes.append("Report flags landslip / slope hazard - check founding and cut/fill recommendations")
    notes.extend(f"geotech concern: {c}" for c in x.concerns)
    return notes
