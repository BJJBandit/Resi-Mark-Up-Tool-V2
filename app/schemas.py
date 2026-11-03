"""Structured-output schemas for each review step.

Every field is required (nullable where the information may be missing) and
every model forbids extra keys, so the JSON schema sent to Claude is strict.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Step 1 - geotechnical report and wind
# ---------------------------------------------------------------------------

SiteClass = Literal[
    "A", "S", "M", "M-D", "H1", "H1-D", "H2", "H2-D", "E", "E-D", "P", "Unknown"
]


class GeotechReview(Strict):
    report_provided: bool
    site_classification: SiteClass = Field(
        description="AS 2870 site class as stated or determined from the report."
    )
    characteristic_surface_movement_ys_mm: float | None
    allowable_bearing_capacity_kpa: float | None = Field(
        description="Allowable bearing pressure at founding depth, if stated."
    )
    recommended_founding_depth_mm: float | None
    founding_material: str | None
    fill_present: bool | None
    fill_depth_mm: float | None
    groundwater_depth_mm: float | None
    trees_or_abnormal_moisture: str | None
    wind_classification: str | None = Field(
        description="AS 4055 wind class, e.g. N2 or C1. Use the user-provided value if given."
    )
    wind_source: Literal["user", "report", "drawings", "not_provided"]
    geotech_recommendations: list[str]
    concerns: list[str] = Field(
        description="Issues the engineer must check (missing data, P sites, fill, slope, etc.)."
    )


# ---------------------------------------------------------------------------
# Step 2 - architectural review / design assessment
# ---------------------------------------------------------------------------


class PageRole(Strict):
    page_number: int = Field(description="1-based page number in the architectural set.")
    title: str
    kind: Literal[
        "site_plan",
        "floor_plan",
        "roof_plan",
        "elevation",
        "section",
        "detail",
        "schedule_or_notes",
        "other",
    ]


class RoofAssessment(Strict):
    roof_form: str = Field(description="e.g. gable, hip, skillion, flat, combination.")
    construction: Literal["pitched_rafters", "trusses", "flat_deck", "skillion_rafters", "mixed", "unknown"]
    pitch_deg: float | None
    cladding: str | None
    roof_mass: Literal["sheet", "tile", "unknown"]
    ceiling: str | None
    deck_areas: list[str] = Field(description="Decks / verandahs / balconies and how they are roofed or framed.")
    notes: list[str]


class WallAssessment(Strict):
    external_wall_type: str = Field(description="e.g. brick veneer, timber frame cladded, double brick, blockwork.")
    internal_wall_type: str
    framing_material: Literal["timber", "light_gauge_steel", "masonry", "concrete_block", "mixed", "unknown"]
    governing_standard: str = Field(description="e.g. AS 1684.2, NASH, AS 3700, AS 4773.")
    storeys: int
    max_wall_height_mm: float | None
    notes: list[str]


class LoadPathStep(Strict):
    element: str
    carries: str
    transfers_to: str


class FootingAssessment(Strict):
    site_class_used: SiteClass
    system: str = Field(description="e.g. waffle raft, stiffened raft, footing slab, strip footings, piers.")
    as2870_basis: str = Field(description="The AS 2870 clause/figure/table basis for the selection.")
    edge_beam: str | None
    internal_beams: str | None
    slab_thickness_mm: float | None
    mesh: str | None
    special_requirements: list[str]


class BracingWall(Strict):
    location: str
    bracing_type: str = Field(description="e.g. AS 1684 Table 8.18 type (g) ply, or masonry pier/engaged pier.")
    capacity_kn: float | None


class WallDesign(Strict):
    wind_classification_used: str | None
    racking_force_summary: str = Field(description="Racking force basis per the relevant standard.")
    bracing_walls: list[BracingWall]
    tie_down: str
    lintels_and_beams: list[str]
    masonry_articulation_or_control_joints: list[str]
    notes: list[str]


class FloorAssessment(Strict):
    system: Literal["slab_on_ground", "suspended_timber", "suspended_steel", "suspended_slab", "mixed", "unknown"]
    description: str
    notes: list[str]


class DesignAssessment(Strict):
    project_summary: str
    pages: list[PageRole]
    roof: RoofAssessment
    walls: WallAssessment
    load_paths: list[LoadPathStep]
    footing: FootingAssessment
    wall_design: WallDesign
    floor: FloorAssessment
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
