"""Claude decides which internal walls carry load, and confirms the roof - by wall ID."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..model import Building, Project, Provenance, Storey
from .llm import UsageTotals, call_structured, image_block, text_block
from .plan_image import labelled_plan_png, page_png


class Flat(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WallLabel(Flat):
    wall_id: str
    load_bearing: bool
    confidence: float = Field(description="0-1.")
    reason: str = Field(description="One sentence: what in the drawings decides it.")


class StoreyLabels(Flat):
    walls: list[WallLabel] = Field(description="One entry for EVERY internal wall ID listed.")
    roof_form: str = Field(description="hip, gable, gable_hip, skillion, flat, combination or unknown.")
    roof_construction: str = Field(description="truss, rafter, mixed or unknown.")
    truss_span_direction: str = Field(description="'x' (across the plan left-right), 'y' (up-down), 'both' or 'unknown'.")
    girder_or_point_loads: list[str] = Field(description="Wall IDs or short descriptions where a girder truss / beam / post lands.")
    notes: list[str] = Field(description="Anything the engineer should check, each one sentence.")


def _wall_listing(storey: Storey) -> str:
    rows = []
    for w in storey.walls:
        dx, dy = w.end.x - w.start.x, w.end.y - w.start.y
        orient = "x" if abs(dx) >= abs(dy) else "y"
        kind = "EXTERNAL" if w.external else "internal"
        rows.append(f"{w.id}: {kind}, {w.length_mm:.0f} long, runs along {orient}, {w.thickness_mm:.0f} thick, {len(w.openings)} opening(s)")
    return "\n".join(rows)


def label_storey(
    pdf_bytes: bytes,
    project: Project,
    building: Building,
    storey: Storey,
    totals: UsageTotals,
    context_pages: list[int] | None = None,
) -> StoreyLabels:
    """One call per storey. Context: the labelled plan image, a text listing of the walls,
    the roof facts already parsed from text, and (optionally) low-res images of the roof
    plan / section pages."""
    internal_ids = [w.id for w in storey.walls if not w.external]
    roof = building.roof
    content = [
        text_block(
            f"Project: {project.project_name or '(unnamed)'}; building '{building.name or building.id}', "
            f"storey '{storey.name}' ({building.storey_count} storey building). Wall height {storey.wall_height_mm} mm.\n"
            f"Roof as read from drawing text: form={roof.form}, construction={roof.construction}, "
            f"pitch={roof.pitch_deg}, cladding={roof.cladding}.\n\n"
            "Plan image below: walls are overdrawn with their IDs (green = external, orange = internal). "
            "The image is for reading room layout, roof lines and architect's annotations only.\n\n"
            f"Walls:\n{_wall_listing(storey)}\n\n"
            "Questions:\n"
            f"1. For EACH internal wall ID ({', '.join(internal_ids) or 'none'}): is it load-bearing? "
            "Treat a wall as load-bearing if roof trusses/rafters, an upper floor, a girder, or a beam "
            "bear on it, or if it supports a ceiling span that cannot be achieved otherwise. For a trussed "
            "roof spanning external wall to external wall, internal walls are usually NOT load-bearing - "
            "say so unless the plan shows otherwise (e.g. a span over ~ 12 m, an L-shaped plan, a girder).\n"
            "2. Roof form and construction, if the text left them unknown; confirm them otherwise.\n"
            "3. The truss/rafter span direction on this plan.\n"
            "4. Where point loads land (girder trusses, beams over wide openings, deck/verandah posts)."
        ),
        image_block(labelled_plan_png(pdf_bytes, storey)),
    ]
    for pno in context_pages or []:
        content.append(text_block(f"Context page {pno} (roof plan / section / elevation), low resolution:"))
        content.append(image_block(page_png(pdf_bytes, pno)))
    return call_structured(content, StoreyLabels, f"label walls {building.id}/{storey.id}", totals)


def apply_storey_labels(building: Building, storey: Storey, labels: StoreyLabels) -> list[str]:
    """Write Claude's answers into the model; returns review notes."""
    notes: list[str] = []
    by_id = {w.id: w for w in storey.walls}
    seen: set[str] = set()
    for lab in labels.walls:
        w = by_id.get(lab.wall_id)
        if w is None:
            notes.append(f"Claude referred to unknown wall {lab.wall_id}")
            continue
        seen.add(lab.wall_id)
        if w.external:
            continue  # external walls are load-bearing by construction; don't let a label flip them
        w.load_bearing = lab.load_bearing
        w.provenance = Provenance(source="claude", page=storey.frame.page_number if storey.frame else None,
                                  confidence=max(0.0, min(1.0, lab.confidence)), note=lab.reason)
        if lab.confidence < 0.6:
            notes.append(f"{w.id}: load-bearing={lab.load_bearing} is a low-confidence call - {lab.reason}")
    for w in storey.walls:
        if not w.external and w.id not in seen:
            notes.append(f"{w.id}: Claude gave no answer - decide load-bearing in review")

    roof = building.roof
    if roof.form == "unknown" and labels.roof_form in {"hip", "gable", "gable_hip", "skillion", "flat", "combination"}:
        roof.form = labels.roof_form  # type: ignore[assignment]
        roof.provenance = Provenance(source="claude", confidence=0.7, note="roof form from plan image")
    if roof.construction == "unknown" and labels.roof_construction in {"truss", "rafter", "mixed"}:
        roof.construction = labels.roof_construction  # type: ignore[assignment]
    if labels.truss_span_direction in {"x", "y"} and roof.span_direction_deg is None:
        roof.span_direction_deg = 0.0 if labels.truss_span_direction == "x" else 90.0
    notes.extend(f"point load: {s}" for s in labels.girder_or_point_loads)
    notes.extend(labels.notes)
    return notes
