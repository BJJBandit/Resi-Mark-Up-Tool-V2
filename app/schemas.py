"""Structured-output schemas for each review step.

Kept deliberately flat and simple: the API compiles each schema into a grammar
with a size limit, and nullable unions / enums are what make it grow. So every
field is required, values that may be missing are plain strings ("not stated"),
and allowed values are listed in descriptions rather than as enums.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


NS = 'Use "not stated" when the documents do not give it.'

# ---------------------------------------------------------------------------
# Step 1 - geotechnical report and wind
# ---------------------------------------------------------------------------


class GeotechReview(Strict):
    report_provided: bool
    site_classification: str = Field(
        description="AS 2870 site class: A, S, M, M-D, H1, H1-D, H2, H2-D, E, E-D, P or Unknown."
    )
    ys: str = Field(description=f"Characteristic surface movement, e.g. '30 mm'. {NS}")
    bearing_capacity: str = Field(description=f"Allowable bearing pressure, e.g. '100 kPa at 500 mm'. {NS}")
    founding: str = Field(description=f"Recommended founding depth and material. {NS}")
    fill: str = Field(description=f"Fill presence and depth. {NS}")
    groundwater: str = Field(description=NS)
    trees_or_moisture: str = Field(description=f"Trees / abnormal moisture conditions. {NS}")
    wind_classification: str = Field(
        description="AS 4055 wind class (e.g. N2, C1). Use the user-provided value if given, else 'not provided'."
    )
    wind_source: str = Field(description="user, report, drawings or not_provided.")
    recommendations: list[str]
    concerns: list[str] = Field(description="Issues the engineer must resolve.")


# ---------------------------------------------------------------------------
# Step 2 - architectural review / design assessment
# ---------------------------------------------------------------------------


class PageRole(Strict):
    page_number: int = Field(description="1-based page number in the architectural set.")
    title: str
    kind: str = Field(description="site plan, floor plan, roof plan, elevation, section, detail, notes or other.")


class LoadPathStep(Strict):
    element: str
    carries: str
    transfers_to: str


class BracingWall(Strict):
    location: str
    bracing_type: str = Field(description="e.g. AS 1684 Table 8.18 type (g) ply.")
    capacity: str = Field(description="e.g. '6.4 kN'.")


class DesignAssessment(Strict):
    project_summary: str
    pages: list[PageRole]

    roof_form: str = Field(description="gable, hip, skillion, flat, combination ...")
    roof_construction: str = Field(description="pitched rafters, trusses, skillion rafters, flat deck or mixed.")
    roof_pitch: str
    roof_cladding: str = Field(description="Cladding and whether sheet or tile.")
    deck_areas: list[str] = Field(description="Decks / verandahs / balconies and how they are framed and roofed.")

    external_wall_type: str = Field(description="e.g. brick veneer, clad timber frame, double brick, blockwork.")
    internal_wall_type: str
    framing_material: str = Field(description="timber, light gauge steel, masonry, concrete block or mixed.")
    governing_standard: str = Field(description="e.g. AS 1684.2, NASH, AS 3700, AS 4773.")
    storeys: int
    wall_height: str

    load_paths: list[LoadPathStep]

    footing_system: str = Field(description="e.g. waffle raft, stiffened raft, strip footings, piers.")
    footing_site_class_used: str
    footing_as2870_basis: str = Field(description="AS 2870 clause / figure / table basis for the selection.")
    footing_details: list[str] = Field(description="Edge beams, internal beams, slab thickness, mesh, special requirements.")

    wind_classification_used: str
    racking_summary: str = Field(description="Racking force basis per the relevant standard.")
    bracing_walls: list[BracingWall]
    tie_down: str
    lintels_and_beams: list[str]
    articulation_and_control_joints: list[str]

    floor_system: str = Field(description="slab on ground, suspended timber, suspended steel, suspended slab or mixed.")
    floor_description: str

    assumptions: list[str]
    engineer_checks: list[str] = Field(description="Items needing confirmation by the design engineer.")


# ---------------------------------------------------------------------------
# Step 3 - mark-up sheets
# ---------------------------------------------------------------------------


class SheetKind(str, Enum):
    LOAD_PATHS = "load_paths"
    ROOF = "roof"
    WALLS = "walls"
    FOOTINGS = "footings"
    FLOOR = "floor"


SHEET_TITLES: dict[SheetKind, str] = {
    SheetKind.LOAD_PATHS: "LOAD PATHS",
    SheetKind.ROOF: "ROOF FRAMING (RAFTERS / DECK)",
    SheetKind.WALLS: "WALL FRAMING, RACKING & BLOCKWORK JOINTS",
    SheetKind.FOOTINGS: "FOOTINGS (AS 2870)",
    SheetKind.FLOOR: "FLOOR FRAMING / SLAB",
}


class Mark(Strict):
    shape: Literal["rect", "line", "arrow", "cloud", "text"]
    x1: float = Field(description="Left / start x, normalised 0-1 across the page image width.")
    y1: float = Field(description="Top / start y, normalised 0-1 down the page image height.")
    x2: float = Field(description="Right / end x, normalised 0-1. Equal to x1 for text.")
    y2: float = Field(description="Bottom / end y, normalised 0-1. Equal to y1 for text.")
    label: str = Field(description="Short tag shown on the drawing, e.g. 'BW1 - 6.4 kN ply'.")
    category: str = Field(description="Legend category, e.g. 'Bracing wall', 'Point load', 'Edge beam'.")


class MarkupSheet(Strict):
    source_page_number: int = Field(description="1-based page of the architectural set to mark up.")
    marks: list[Mark]
    notes: list[str] = Field(description="General notes printed in the notes panel of the sheet.")
