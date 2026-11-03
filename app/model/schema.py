"""Pydantic schema for the structural model (see package docstring)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# Provenance - every reviewed value says where it came from
# ---------------------------------------------------------------------------

Source = Literal["pdf_text", "pdf_vector", "claude", "geotech", "user", "default", "derived"]


class Provenance(Strict):
    source: Source
    page: int | None = None
    confidence: float = Field(1.0, ge=0, le=1)
    note: str = ""


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------

SiteClass = Literal["A", "S", "M", "M-D", "H1", "H1-D", "H2", "H2-D", "E", "E-D", "P", "Unknown"]
WindClass = Literal["N1", "N2", "N3", "N4", "N5", "N6", "C1", "C2", "C3", "C4"]


class WindInputs(Strict):
    """AS 4055 Section 2 inputs. `stated_class` is what the architect / geotech / user
    says; the engine derives its own class from the inputs and reports any difference."""

    region: Literal["A", "B", "C", "D"] = "A"
    terrain_category: Literal["TC1", "TC2", "TC2.5", "TC3"] | None = None
    shielding: Literal["FS", "PS", "NS"] | None = None
    topographic_class: Literal["T0", "T1", "T2", "T3", "T4", "T5"] | None = None
    site_slope_deg: float | None = None
    stated_class: WindClass | None = None
    stated_source: Literal["architect", "geotech", "user"] | None = None


class Geotech(Strict):
    report_provided: bool = False
    site_class: SiteClass = "Unknown"
    ys_mm: float | None = None
    bearing_kpa: float | None = None
    founding_depth_mm: float | None = None
    founding_material: str = ""
    rock_depth_mm: float | None = None
    fill_present: bool | None = None
    fill_depth_mm: float | None = None
    groundwater: str = ""
    trees_or_abnormal_moisture: bool | None = None
    landslip_or_slope_hazard: bool | None = None
    recommendations: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)


class Site(Strict):
    address: str = ""
    title_reference: str = ""
    state: str = "TAS"
    geotech: Geotech = Field(default_factory=Geotech)
    wind: WindInputs = Field(default_factory=WindInputs)
    bal: str | None = None
    climate_zone: int | None = None
    corrosion_environment: Literal["low", "medium", "high", "severe"] | None = None
    provenance: dict[str, Provenance] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class Pt(Strict):
    x: float
    y: float


class PlanFrame(Strict):
    """Maps building-local mm to PDF page points for one plan view.

    page_pt = origin + R(rotation) * (mm * pts_per_mm)
    """

    page_number: int
    scale_denominator: float = Field(description="100 for 1:100.")
    origin: Pt = Field(description="Page point (PDF user space, origin bottom-left) of building (0,0).")
    rotation_deg: float = 0.0
    sheet_size: str = "A3"

    @property
    def pts_per_mm(self) -> float:
        # 1 mm on paper = 72/25.4 pt; 1 mm real = 1/scale mm on paper
        return (72.0 / 25.4) / self.scale_denominator


class Polygon(Strict):
    points: list[Pt] = Field(min_length=3)


# ---------------------------------------------------------------------------
# Building elements
# ---------------------------------------------------------------------------

WallConstruction = Literal[
    "timber_stud", "brick_veneer", "double_brick", "blockwork", "blockwork_corefilled",
    "steel_frame", "concrete", "other",
]
OpeningKind = Literal["window", "door", "sliding_door", "garage_door", "opening", "unknown"]


class Opening(Strict):
    id: str
    offset_mm: float = Field(description="From wall start along the wall to the opening's near jamb.")
    width_mm: float
    head_height_mm: float | None = None
    sill_height_mm: float | None = None
    kind: OpeningKind = "unknown"
    tag: str = Field("", description="Architect's tag, e.g. A1524, SD2121.")
    provenance: Provenance = Field(default_factory=lambda: Provenance(source="pdf_vector"))


class Wall(Strict):
    id: str
    start: Pt
    end: Pt
    thickness_mm: float
    construction: WallConstruction = "timber_stud"
    external: bool
    load_bearing: bool | None = Field(None, description="None = not yet decided; the planner asks.")
    height_mm: float | None = None
    openings: list[Opening] = Field(default_factory=list)
    bracing_eligible: bool = True
    notes: str = ""
    provenance: Provenance = Field(default_factory=lambda: Provenance(source="pdf_vector"))

    @property
    def length_mm(self) -> float:
        return ((self.end.x - self.start.x) ** 2 + (self.end.y - self.start.y) ** 2) ** 0.5


class Column(Strict):
    id: str
    at: Pt
    continuous_full_height: bool = False
    supports: list[str] = Field(default_factory=list, description="IDs of beams / walls above.")
    section: str | None = Field(None, description="Filled by the design stage, e.g. '89x3.5 SHS'.")
    provenance: Provenance = Field(default_factory=lambda: Provenance(source="pdf_vector"))


FloorKind = Literal[
    "slab_on_ground", "suspended_timber", "suspended_timber_on_steel", "suspended_slab",
    "bondek_slab", "unknown",
]


class Floor(Strict):
    kind: FloorKind = "unknown"
    level_rl: float | None = None
    outline: Polygon | None = None
    tiled_areas: list[Polygon] = Field(default_factory=list)
    setdowns: list[Polygon] = Field(default_factory=list)
    thickness_mm: float | None = None


class Deck(Strict):
    id: str
    outline: Polygon
    level_rl: float | None = None
    attached_wall_ids: list[str] = Field(default_factory=list)
    post_locations: list[Pt] = Field(default_factory=list)
    roofed: bool = False


RoofForm = Literal["hip", "gable", "gable_hip", "skillion", "flat", "combination", "unknown"]


class Roof(Strict):
    form: RoofForm = "unknown"
    construction: Literal["truss", "rafter", "mixed", "unknown"] = "unknown"
    pitch_deg: float | None = None
    cladding: Literal["sheet", "tile", "unknown"] = "unknown"
    eaves_mm: float | None = None
    outline: Polygon | None = None
    ridge_lines: list[tuple[Pt, Pt]] = Field(default_factory=list)
    span_direction_deg: float | None = Field(None, description="Truss / rafter span direction, 0 = +x.")
    girder_points: list[Pt] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=lambda: Provenance(source="claude"))


class Storey(Strict):
    id: str
    name: Literal["subfloor", "ground", "first", "second"] = "ground"
    level_rl: float | None = None
    wall_height_mm: float = 2400
    ceiling_height_mm: float | None = None
    walls: list[Wall] = Field(default_factory=list)
    columns: list[Column] = Field(default_factory=list)
    floor: Floor = Field(default_factory=Floor)
    frame: PlanFrame | None = Field(None, description="Plan page this storey was taken from.")

    def external_walls(self) -> list[Wall]:
        return [w for w in self.walls if w.external]


class Building(Strict):
    id: str
    name: str = ""
    ncc_class: Literal["1a", "1b", "10a", "10b"] = "1a"
    storeys: list[Storey] = Field(default_factory=list)
    roof: Roof = Field(default_factory=Roof)
    decks: list[Deck] = Field(default_factory=list)
    external_wall_construction: WallConstruction = "brick_veneer"
    internal_wall_construction: WallConstruction = "timber_stud"
    footprint: Polygon | None = None

    @property
    def storey_count(self) -> int:
        return len([s for s in self.storeys if s.name != "subfloor"])


# ---------------------------------------------------------------------------
# Pages and review state
# ---------------------------------------------------------------------------

PageKind = Literal[
    "cover", "site_plan", "floor_plan", "roof_plan", "elevation", "section", "detail",
    "drainage", "electrical", "notes", "schedule", "other",
]


class PageInfo(Strict):
    number: int
    title: str = ""
    kind: PageKind = "other"
    scale_denominator: float | None = None
    sheet_size: str | None = None
    is_vector: bool = True
    building_id: str | None = None
    storey_name: str | None = None


class ReviewEdit(Strict):
    at: datetime
    by: str = ""
    path: str = Field(description="JSON pointer-ish path of the field changed.")
    old: str = ""
    new: str = ""


class ReviewState(Strict):
    status: Literal["draft", "reviewed"] = "draft"
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    edits: list[ReviewEdit] = Field(default_factory=list)
    needs_attention: list[str] = Field(
        default_factory=list, description="Things extraction could not settle; shown first in review."
    )


class Project(Strict):
    """Top-level structural model for one job."""

    schema_version: int = 1
    project_name: str = ""
    job_no: str = ""
    client: str = ""
    site: Site = Field(default_factory=Site)
    buildings: list[Building] = Field(default_factory=list)
    pages: list[PageInfo] = Field(default_factory=list)
    construction_nominated: str = Field("", description="Engineer's input, e.g. 'brick veneer, timber frame'.")
    engineer_notes: str = ""
    review: ReviewState = Field(default_factory=ReviewState)

    def building(self, building_id: str) -> Building:
        for b in self.buildings:
            if b.id == building_id:
                return b
        raise KeyError(building_id)


class ModelIssue(str, Enum):
    """Reasons a model can't go to design yet."""

    NO_SCALE = "no_scale"
    NO_WALLS = "no_walls"
    SITE_CLASS_UNKNOWN = "site_class_unknown"
    WIND_UNKNOWN = "wind_unknown"
    LOAD_BEARING_UNDECIDED = "load_bearing_undecided"


def readiness(project: Project) -> list[ModelIssue]:
    """What still blocks the design stage."""
    issues: list[ModelIssue] = []
    if project.site.geotech.site_class == "Unknown":
        issues.append(ModelIssue.SITE_CLASS_UNKNOWN)
    w = project.site.wind
    if w.stated_class is None and not (w.terrain_category and w.shielding and w.topographic_class):
        issues.append(ModelIssue.WIND_UNKNOWN)
    for b in project.buildings:
        for s in b.storeys:
            if s.frame is None:
                issues.append(ModelIssue.NO_SCALE)
            if not s.walls:
                issues.append(ModelIssue.NO_WALLS)
            if any(wall.load_bearing is None for wall in s.walls if not wall.external):
                issues.append(ModelIssue.LOAD_BEARING_UNDECIDED)
    # de-duplicate, keep order
    seen: set[ModelIssue] = set()
    return [i for i in issues if not (i in seen or seen.add(i))]
