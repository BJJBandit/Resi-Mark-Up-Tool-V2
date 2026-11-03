"""Step 5 - wire the page scan, dimensions and walls into the `Project` model.

`extract_project(pdf_bytes)` returns the model plus a plain-dict diagnostics record.
No LLM is involved. Anything the geometry could not settle goes into
`project.review.needs_attention` for the engineer, never guessed:

* no scale found, or the measured scale disagreeing with the printed one by > 2 %;
* a page that is a raster image rather than vectors;
* a wall whose thickness matches no nominal size;
* an opening tag that no opening claimed;
* a plan whose external walls do not close;
* internal walls, whose `load_bearing` is left `None` for the next stage.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import pymupdf

from app.model.schema import (
    Building,
    Opening,
    PageInfo,
    PlanFrame,
    Polygon as SchemaPolygon,
    Project,
    Provenance,
    Pt,
    Site,
    Storey,
    Wall,
)

from .dimensions import DimensionResult, analyse_dimensions
from .geom import pts_per_mm
from .pdf_pages import PageScan, ScaleMention, scan_page, title_block_scale
from .vector import VectorSet, extract_vectors
from .walls import Plan, Run, WallResult, detect_walls

SCALE_DISAGREE_PCT = 2.0
BRICK_VENEER_RANGE_MM = (235, 285)


@dataclass
class PageExtraction:
    scan: PageScan
    vectors: VectorSet | None = None
    dims: DimensionResult | None = None
    walls: WallResult | None = None
    scale: float | None = None
    scale_source: str = ""  # "measured" | "title_block" | "text" | ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# scale choice
# ---------------------------------------------------------------------------


def choose_scale(scan: PageScan, dims: DimensionResult | None) -> tuple[float | None, str, list[str]]:
    """Measured scale when enough dimensions agree; else the title-block scale; else
    the first standard scale printed anywhere. Reports disagreement."""
    notes: list[str] = []
    text_scale: ScaleMention | None = title_block_scale(scan)
    source_text = "title_block"
    if text_scale is None and scan.scales:
        text_scale = scan.scales[0]
        source_text = "text"
    measured = dims.scale_measured if dims and dims.n_agree >= 3 else None
    if measured is not None:
        chosen = float(round(measured))
        if text_scale is not None:
            diff = abs(measured - text_scale.denominator) / text_scale.denominator * 100
            if diff > SCALE_DISAGREE_PCT:
                notes.append(
                    f"page {scan.number}: printed scale 1:{text_scale.denominator:g} but dimensions measure "
                    f"1:{measured:.1f} ({dims.n_agree} of {dims.n_votes} agree); using the measured scale"
                )
        return chosen, "measured", notes
    if text_scale is not None:
        if dims and dims.n_votes:
            notes.append(f"page {scan.number}: dimensions did not confirm the printed scale 1:{text_scale.denominator:g}")
        return text_scale.denominator, source_text, notes
    notes.append(f"page {scan.number}: no scale found in text and no usable dimension strings")
    return None, "", notes


# ---------------------------------------------------------------------------
# site / project text
# ---------------------------------------------------------------------------

_SITE_CLASS_RE = re.compile(r"(?<![A-Z0-9])(A|S|M|M-D|H1|H1-D|H2|H2-D|E|E-D|P)(?![A-Z0-9])")
_WIND_RE = re.compile(r"\b([NC][1-6])\b")
_BAL_RE = re.compile(r"\b(LOW|12\.5|19|29|40|FZ)\b")
_CORROSION = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high", "SEVERE": "severe"}
_JOB_RE = re.compile(r"\b([A-Z]{1,4}\d{3,}(?:-[A-Z0-9]+)*)\b")


def _first(pages: list[PageExtraction], key: str) -> tuple[str, int] | None:
    """Value of a title-block key, cover page first."""
    for pe in pages:
        v = pe.scan.title_block.get(key)
        if v:
            return v, pe.scan.number
    return None


def fill_site_and_project(project: Project, pages: list[PageExtraction], notes: list[str]) -> None:
    site: Site = project.site

    hit = _first(pages, "soil_classification")
    if hit:
        m = _SITE_CLASS_RE.search(hit[0].upper().replace("CLASS", " "))
        if m:
            site.geotech.site_class = m.group(1)  # type: ignore[assignment]
            site.provenance["geotech.site_class"] = Provenance(source="pdf_text", page=hit[1], note=f"'{hit[0]}'")
        else:
            notes.append(f"page {hit[1]}: soil classification text '{hit[0]}' is not a site class")

    hit = _first(pages, "wind_class")
    if hit:
        m = _WIND_RE.search(hit[0].upper())
        if m:
            site.wind.stated_class = m.group(1)  # type: ignore[assignment]
            site.wind.stated_source = "architect"
            site.provenance["wind.stated_class"] = Provenance(source="pdf_text", page=hit[1], note=f"'{hit[0]}'")

    hit = _first(pages, "bal")
    if hit:
        m = _BAL_RE.search(hit[0].upper())
        if m:
            site.bal = m.group(1)
            site.provenance["bal"] = Provenance(source="pdf_text", page=hit[1])

    hit = _first(pages, "corrosion_environment")
    if hit and hit[0].strip().upper() in _CORROSION:
        site.corrosion_environment = _CORROSION[hit[0].strip().upper()]  # type: ignore[assignment]
        site.provenance["corrosion_environment"] = Provenance(source="pdf_text", page=hit[1])

    hit = _first(pages, "climate_zone")
    if hit and hit[0].strip().isdigit() and 1 <= int(hit[0]) <= 8:
        site.climate_zone = int(hit[0])
        site.provenance["climate_zone"] = Provenance(source="pdf_text", page=hit[1])

    hit = _first(pages, "title_reference")
    if hit:
        site.title_reference = hit[0]

    hit = _first(pages, "client")
    if hit:
        project.client = hit[0]

    hit = _first(pages, "project")
    if hit:
        text = hit[0]
        m = re.match(r"^([A-Z]{1,4}\d{3,}[-\w]*)\s*[-:]\s*(.+)$", text)
        if m:
            project.job_no, text = m.group(1), m.group(2)
        project.project_name = text
        if re.search(r"\d.*\b(ROAD|RD|STREET|ST|AVENUE|AVE|DRIVE|DR|COURT|CT|CRESCENT|CRES|PLACE|PL|LANE|HIGHWAY|HWY)\b", text.upper()):
            site.address = text

    if not project.job_no:
        hit = _first(pages, "job_no")
        if hit:
            project.job_no = hit[0]
    if not project.job_no and pages:
        cover = pages[0].scan.text
        m = re.search(r"Job\s*(?:No\.?|Number)\s*:?\s*([A-Z]{0,4}\d{3,}[-\w]*)", cover, re.I)
        if m:
            project.job_no = m.group(1)
        else:
            m = _JOB_RE.search(cover)
            if m and "-" in m.group(1):
                project.job_no = m.group(1)


# ---------------------------------------------------------------------------
# plans -> buildings / storeys / walls
# ---------------------------------------------------------------------------


def _storey_name(label: str, title: str) -> str:
    text = f"{label} {title}".upper()
    if re.search(r"SECOND|LEVEL\s*2|\bL2\b", text):
        return "second"
    if re.search(r"FIRST|UPPER|LEVEL\s*1|\bL1\b|\bFF\b", text):
        return "first"
    if re.search(r"SUBFLOOR|SUB-FLOOR|LOWER GROUND|BASEMENT|UNDERCROFT", text):
        return "subfloor"
    return "ground"


def _building_key(label: str, title: str) -> tuple[str, str]:
    text = f"{label} {title}".upper()
    m = re.search(r"UNIT\s*(\d+)", text)
    if m:
        return f"unit{m.group(1)}", f"Unit {m.group(1)}"
    for word, name in (("GARAGE", "Garage"), ("SHED", "Shed"), ("EXISTING", "Existing")):
        if word in text:
            return word.lower(), name
    return "house", "House"


def _wall_from_run(run: Run, wall_id: str, origin: tuple[float, float], ppm: float, notes: list[str], where: str) -> Wall:
    ox, oy = origin

    def mm(p: tuple[float, float]) -> Pt:
        return Pt(x=round((p[0] - ox) / ppm, 1), y=round((p[1] - oy) / ppm, 1))

    measured = run.thickness / ppm
    thickness = run.nominal_mm if run.nominal_mm is not None else round(measured)
    if run.nominal_mm is None:
        notes.append(f"{where}: wall {wall_id} is {measured:.0f} mm thick, which matches no standard wall; check it is a wall")
    construction = "brick_veneer" if run.external and BRICK_VENEER_RANGE_MM[0] <= measured <= BRICK_VENEER_RANGE_MM[1] else "timber_stud"
    openings: list[Opening] = []
    for i, o in enumerate(run.openings, 1):
        measured_w = (o.hi - o.lo) / ppm
        width = o.tag.width_mm if (o.tag and o.tag.width_mm) else round(measured_w)
        openings.append(
            Opening(
                id=f"{wall_id}-O{i}",
                offset_mm=round((o.lo - run.lo) / ppm, 1),
                width_mm=width,
                head_height_mm=None,
                kind=o.kind if o.kind in ("window", "door", "sliding_door", "garage_door", "opening", "unknown") else "unknown",
                tag=o.tag_text,
                provenance=Provenance(
                    source="pdf_vector",
                    confidence=o.confidence,
                    note=f"gap measured {measured_w:.0f} mm; evidence: {o.evidence}"
                    + (f"; tag height {o.tag.height_mm:.0f} mm" if o.tag and o.tag.height_mm else ""),
                ),
            )
        )
    return Wall(
        id=wall_id,
        start=mm(run.point(run.lo)),
        end=mm(run.point(run.hi)),
        thickness_mm=float(thickness),
        construction=construction,  # type: ignore[arg-type]
        external=bool(run.external),
        load_bearing=True if run.external else None,
        openings=openings,
        notes="" if run.nominal_mm is not None else f"measured {measured:.0f} mm",
        provenance=Provenance(source="pdf_vector", confidence=1.0 if run.pieces else 0.6,
                              note="" if run.pieces else "no wall bar drawn; inferred from window lines"),
    )


def _storey_from_plan(plan: Plan, pe: PageExtraction, storey_id: str, name: str, notes: list[str]) -> Storey:
    ppm = pts_per_mm(pe.scale or 100.0)
    origin = (plan.bbox[0], plan.bbox[1])
    frame = PlanFrame(
        page_number=pe.scan.number,
        scale_denominator=pe.scale or 100.0,
        origin=Pt(x=round(origin[0], 2), y=round(origin[1], 2)),
        rotation_deg=0.0,
        sheet_size=pe.scan.sheet_size,
    )
    where = f"page {pe.scan.number} {plan.label}"
    ordered = sorted(plan.runs, key=lambda r: (not r.external, r.axis, r.pos, r.lo))
    walls = [_wall_from_run(r, f"{storey_id}-W{i:02d}", origin, ppm, notes, where) for i, r in enumerate(ordered, 1)]
    if not plan.closed:
        notes.append(f"{where}: external walls do not form a closed boundary; check for a missing wall or open side")
    return Storey(id=storey_id, name=name, walls=walls, frame=frame)  # type: ignore[arg-type]


def _footprint(plan: Plan, pe: PageExtraction) -> SchemaPolygon | None:
    ppm = pts_per_mm(pe.scale or 100.0)
    ox, oy = plan.bbox[0], plan.bbox[1]
    ring = plan.outline.simplify(0.75).exterior.coords[:-1]
    pts = [Pt(x=round((x - ox) / ppm, 1), y=round((y - oy) / ppm, 1)) for x, y in ring]
    return SchemaPolygon(points=pts) if len(pts) >= 3 else None


def build_buildings(pages: list[PageExtraction], notes: list[str]) -> list[Building]:
    buildings: dict[str, Building] = {}
    for pe in pages:
        if not pe.walls:
            continue
        for plan in pe.walls.plans:
            key, bname = _building_key(plan.label, pe.scan.title)
            storey_name = _storey_name(plan.label, pe.scan.title)
            b = buildings.get(key)
            if b is not None and any(s.name == storey_name for s in b.storeys):
                n = 2
                while f"{key}_{n}" in buildings:
                    n += 1
                notes.append(
                    f"page {pe.scan.number}: a second '{storey_name}' plan for {bname} ('{plan.label}'); "
                    f"kept as building {key}_{n} - merge or delete it"
                )
                key, bname = f"{key}_{n}", f"{bname} ({plan.label})"
                b = None
            if b is None:
                b = Building(id=key, name=bname, ncc_class="10a" if key in ("garage", "shed") else "1a")
                buildings[key] = b
            storey_id = f"{key}-{storey_name}"
            storey = _storey_from_plan(plan, pe, storey_id, storey_name, notes)
            b.storeys.append(storey)
            if storey_name == "ground" or b.footprint is None:
                b.footprint = _footprint(plan, pe)
            ext = [w for w in storey.walls if w.external]
            if ext and any(BRICK_VENEER_RANGE_MM[0] <= w.thickness_mm <= BRICK_VENEER_RANGE_MM[1] for w in ext):
                b.external_wall_construction = "brick_veneer"
            if any(not w.external for w in storey.walls):
                notes.append(f"{storey_id}: {sum(1 for w in storey.walls if not w.external)} internal walls need a load-bearing decision")
    order = {"subfloor": 0, "ground": 1, "first": 2, "second": 3}
    for b in buildings.values():
        b.storeys.sort(key=lambda s: order[s.name])
    return list(buildings.values())


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def extract_pages(doc: pymupdf.Document, plan_pages: set[int] | None = None) -> list[PageExtraction]:
    """Scan every page; run geometry on floor-plan pages (or the given page numbers)."""
    out: list[PageExtraction] = []
    for page in doc:
        # counting drawings is cheap; full vector extraction is only done on plan pages
        scan = scan_page(page, drawing_count=len(page.get_cdrawings()))
        pe = PageExtraction(scan=scan)
        is_plan = (scan.number in plan_pages) if plan_pages is not None else (scan.kind == "floor_plan")
        if is_plan and scan.is_vector:
            vectors = pe.vectors = extract_vectors(page)
            pe.dims = analyse_dimensions(scan, vectors)
            pe.scale, pe.scale_source, scale_notes = choose_scale(scan, pe.dims)
            pe.notes.extend(scale_notes)
            pe.walls = detect_walls(scan, vectors, pe.scale, [d.word.bbox for d in pe.dims.dims])
            pe.notes.extend(pe.walls.notes)
            for tag in pe.walls.unmatched_tags:
                pe.notes.append(f"page {scan.number}: opening tag '{tag}' was not matched to any opening")
        elif is_plan:
            pe.notes.append(f"page {scan.number}: floor plan is a raster image, not vectors; nothing can be measured from it")
        else:
            pe.scale, pe.scale_source, _ = choose_scale(scan, None)
        out.append(pe)
    return out


def extract_project(pdf_bytes: bytes, plan_pages: set[int] | None = None) -> tuple[Project, dict]:
    t0 = time.time()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = extract_pages(doc, plan_pages)
    notes: list[str] = []
    project = Project()
    fill_site_and_project(project, pages, notes)
    project.buildings = build_buildings(pages, notes)

    for pe in pages:
        info = PageInfo(
            number=pe.scan.number,
            title=pe.scan.title,
            kind=pe.scan.kind,  # type: ignore[arg-type]
            scale_denominator=pe.scale,
            sheet_size=pe.scan.sheet_size,
            is_vector=pe.scan.is_vector,
        )
        if pe.walls and pe.walls.plans:
            key, _ = _building_key(pe.walls.plans[0].label, pe.scan.title)
            info.building_id = key
            info.storey_name = _storey_name(pe.walls.plans[0].label, pe.scan.title)
        project.pages.append(info)
        notes.extend(pe.notes)
        if not pe.scan.is_vector and pe.scan.kind not in ("cover", "notes", "schedule"):
            notes.append(f"page {pe.scan.number}: not a vector page ({pe.scan.kind}); it cannot be measured")

    if not project.buildings:
        notes.append("no floor plan with walls was found on any page")
    if project.site.geotech.site_class == "Unknown":
        notes.append("site class not found in the drawings; take it from the geotech report")
    if project.site.wind.stated_class is None:
        notes.append("wind class not found in the drawings")
    project.review.needs_attention = list(dict.fromkeys(notes))

    diagnostics = {
        "seconds": round(time.time() - t0, 2),
        "pages": [_page_diag(pe) for pe in pages],
    }
    return project, diagnostics


def _page_diag(pe: PageExtraction) -> dict:
    d: dict = {
        "page": pe.scan.number,
        "kind": pe.scan.kind,
        "title": pe.scan.title,
        "sheet": pe.scan.sheet_size,
        "is_vector": pe.scan.is_vector,
        "drawings": pe.scan.drawing_count,
        "scales_in_text": [s.text for s in pe.scan.scales],
        "scale_used": pe.scale,
        "scale_source": pe.scale_source,
    }
    if pe.dims:
        d["dimensions"] = {
            "votes": pe.dims.n_votes,
            "agree": pe.dims.n_agree,
            "measured_scale": round(pe.dims.scale_measured, 2) if pe.dims.scale_measured else None,
            "confidence": round(pe.dims.confidence, 2),
            "secondary_scales": pe.dims.secondary_scales,
            "extents_mm": {"x": pe.dims.extents_mm("h"), "y": pe.dims.extents_mm("v")},
            "chains": [
                {"axis": c.axis, "n": len(c.dims), "overall_mm": c.overall.value_mm if c.overall else None,
                 "sum_mismatch_pct": round(c.sum_mismatch_pct, 2) if c.sum_mismatch_pct is not None else None}
                for c in pe.dims.chains if c.overall
            ],
        }
    if pe.walls:
        ppm = pts_per_mm(pe.scale or 100.0)
        d["walls"] = {
            "mode": pe.walls.mode,
            "heavy_pen": pe.walls.heavy_width,
            "bars": len(pe.walls.bars),
            "dropped_clusters": pe.walls.dropped_clusters,
            "unmatched_tags": pe.walls.unmatched_tags,
            "plans": [
                {
                    "label": p.label,
                    "walls": len(p.runs),
                    "external": sum(1 for r in p.runs if r.external),
                    "closed": p.closed,
                    "openings": sum(len(r.openings) for r in p.runs),
                    "extents_mm": {"x": round((p.bbox[2] - p.bbox[0]) / ppm), "y": round((p.bbox[3] - p.bbox[1]) / ppm)},
                }
                for p in pe.walls.plans
            ],
        }
    return d
