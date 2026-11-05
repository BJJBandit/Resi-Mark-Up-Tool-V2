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


class DimCheck(Strict):
    item: str = Field(description="What was compared, e.g. 'OVERALL LENGTH - NORTH ELEVATION vs PLAN', "
                      "'ALFRESCO ROOF EXTENT - EAST ELEVATION vs PLAN', 'WALL HEIGHT (FFL TO CEILING)'.")
    plan: str = Field(description="Value on the proposed plan view(s) (or roof plan), with units, or 'not shown'.")
    elevation: str = Field(description="Value on the proposed elevation, with units, or 'not shown'.")
    status: str = Field(description="match, mismatch, or not shown.")
    note: str = Field(description="Short explanation; for a mismatch say which drawing is likely wrong if clear.")


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
    dimension_crosscheck: list[DimCheck] = Field(
        description="Proposed plan view dimensions compared with the proposed elevations, including "
        "the extent of closed / covered roof areas."
    )
    opening_crosscheck: list[str] = Field(
        description="Each discrepancy found when cross-referencing the plan and elevation views for "
        "openings (code, size, position, which elevation / wall), one per line. Empty if they agree."
    )


# ---------------------------------------------------------------------------
# Step 3 - mark-up sheets
# ---------------------------------------------------------------------------


class SheetKind(str, Enum):
    LOAD_PATHS = "load_paths"
    ROOF = "roof"
    WALLS = "walls"
    FOOTINGS = "footings"  # footings and floor framing / slab on one sheet
    ARTICULATION = "articulation"  # masonry jobs only; drawn on the elevations


SHEET_TITLES: dict[SheetKind, str] = {
    SheetKind.LOAD_PATHS: "LOAD PATH PLAN",
    SheetKind.ROOF: "ROOF FRAMING PLAN",
    SheetKind.WALLS: "WALL FRAMING PLAN",
    SheetKind.FOOTINGS: "FOOTING & FLOOR PLAN",
    SheetKind.ARTICULATION: "ARTICULATION JOINT ELEVATIONS",
}


class Point(Strict):
    x: float = Field(description="Normalised 0-1 across the source page image width.")
    y: float = Field(description="Normalised 0-1 down the source page image height.")


class Element(Strict):
    kind: Literal["member", "span", "pier", "column", "bracing", "cloud", "callout", "tag"] = Field(
        description=(
            "member: thick line along a structural member (bearer, lintel, beam, footing) from start to end. "
            "span: double-arrow line showing span / load direction (joists, rafters, trusses). "
            "pier: bored pier / pad at start. column: post / column at start. "
            "bracing: line along a bracing wall panel from start to end. "
            "cloud: revision cloud around the box start-end. "
            "callout: note text with a leader arrow pointing at start. "
            "tag: text label placed at start."
        )
    )
    tag: str = Field(
        description="Schedule tag, e.g. B1, J1, L2, VB1, R1, C1, DS, EB1, P1, LP1. "
        "For bracing use the type and length, e.g. 'h(b) - 900', 'g - 600', 'd - 2400'. Empty for callouts."
    )
    start: Point
    end: Point = Field(description="End point for member / span / bracing / cloud; same as start otherwise.")
    text: str = Field(description="Callout or tag text in UPPERCASE; empty for other kinds.")


class ScheduleItem(Strict):
    tag: str = Field(description="e.g. B1, L2, C1, or a bracing type such as h(b), g, d.")
    text: str = Field(description="UPPERCASE description, e.g. '2/240x45 F7 TP CONTINUOUS BEARER'.")


class Box(Strict):
    x1: float
    y1: float
    x2: float
    y2: float


class MarkupSheet(Strict):
    source_page_number: int = Field(description="1-based page of the architectural set to mark up (a plan).")
    plan_region: Box = Field(
        description="Normalised box on that page around the plan drawing only (exclude the architect's "
        "title block, notes and other views). This is cropped onto the mark-up sheet."
    )
    classification: str = Field(
        description="e.g. 'AS2870 2011 SITE CLASSIFICATION: M' or 'AS4055 2021 WIND CLASSIFICATION: N3'."
    )
    schedule_title: str = Field(description="e.g. 'FLOOR FRAMING SCHEDULE:', 'LINTEL SCHEDULE:'.")
    schedule: list[ScheduleItem]
    legend_title: str = Field(description="e.g. 'BRACING LEGEND:'; empty if no legend.")
    legend: list[ScheduleItem]
    elements: list[Element]
    notes: list[str] = Field(description="Sheet notes in UPPERCASE, each starting with '-'.")


# ---------------------------------------------------------------------------
# Reference documents - which pages of a standard / guide the review needs
# ---------------------------------------------------------------------------


class RefSection(Strict):
    title: str = Field(description="Section / clause / table title, e.g. 'Section 8 Racking and shear force'.")
    pdf_pages: str = Field(description="PDF page numbers (the 'p' numbers in the page map), e.g. '45-60, 72'.")
    why: str = Field(description="One line: which part of this review needs it.")


class RefSelection(Strict):
    sections: list[RefSection]


class RefPageText(Strict):
    pdf_page: int = Field(description="PDF page number of the original document (as given in the request).")
    text: str = Field(description="Complete transcription of the page (see instructions).")


class RefTranscript(Strict):
    pages: list[RefPageText]
