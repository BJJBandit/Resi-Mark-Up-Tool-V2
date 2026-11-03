"""Claude API calls for each review step."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .pdf import PageImage
from .schemas import (
    SHEET_TITLES,
    DesignAssessment,
    GeotechReview,
    MarkupSheet,
    SheetKind,
)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "high")

client = anthropic.Anthropic()

T = TypeVar("T", bound=BaseModel)

SYSTEM = """You are assisting an Australian structural engineer with residential \
(Class 1 and 10) building design. You review geotechnical reports and architectural \
drawings and prepare preliminary structural mark-ups.

Work to the current Australian Standards and NCC: AS 2870 (residential slabs and \
footings), AS 4055 (wind loads for housing), AS 1684 (timber framing), NASH Standard \
(light gauge steel), AS 3700 / AS 4773 (masonry), AS 1170 series, AS 3600 where relevant.

Rules:
- Base every finding on what is actually in the documents. When something is not \
shown, say so and record a reasonable assumption rather than inventing detail.
- Quote clause / table / figure references where you rely on them.
- Your output is a preliminary design aid. Anything that needs engineering judgement \
or site confirmation goes in the concerns / engineer_checks lists.
- Use metric units (mm, kPa, kN)."""


class ClaudeError(RuntimeError):
    pass


def _schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()


def _request(messages: list[dict], max_tokens: int, schema: dict | None) -> str:
    """One streamed request (avoids HTTP timeouts); returns the text output."""
    output_config: dict = {"effort": EFFORT}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config=output_config,
        messages=messages,
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        detail = getattr(message, "stop_details", None)
        raise ClaudeError(f"Claude declined the request: {detail}")
    if message.stop_reason == "max_tokens":
        raise ClaudeError("Claude's response was cut off (max_tokens reached).")
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    if not text:
        raise ClaudeError("Claude returned no text content.")
    return text


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in response")
    return text[start : end + 1]


def _call(content: list[dict], out: type[T], max_tokens: int = 32000) -> T:
    """Structured-output request. If the API rejects the schema (e.g. compiled
    grammar too large), fall back to plain JSON validated locally."""
    schema = _schema(out)
    messages = [{"role": "user", "content": content}]
    try:
        return out.model_validate_json(_request(messages, max_tokens, schema))
    except anthropic.BadRequestError as e:
        if "grammar" not in str(e).lower() and "schema" not in str(e).lower():
            raise

    instruction = (
        "Respond with ONLY a single JSON object (no prose, no code fences) that "
        "validates against this JSON schema. Every property is required.\n"
        + json.dumps(schema)
    )
    messages = [{"role": "user", "content": [*content, {"type": "text", "text": instruction}]}]
    last_error: Exception | None = None
    for _ in range(2):
        text = _request(messages, max_tokens, None)
        try:
            return out.model_validate_json(_extract_json(text))
        except (ValueError, ValidationError) as err:
            last_error = err
            messages += [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": f"That JSON was invalid: {err}. Reply with the corrected JSON object only.",
                },
            ]
    raise ClaudeError(f"Claude's JSON did not match the expected structure: {last_error}")


# ---------------------------------------------------------------------------
# Reference documents (standards, guides, codes) supplied by the engineer
# ---------------------------------------------------------------------------


@dataclass
class Reference:
    title: str
    file_id: str


def upload_references(files: list[tuple[str, bytes]]) -> list[Reference]:
    """Upload once via the Files API so every step can cite them without resending bytes."""
    refs: list[Reference] = []
    try:
        for name, data in files:
            meta = client.files.upload(file=(name, data, "application/pdf"))
            refs.append(Reference(title=name.rsplit(".", 1)[0], file_id=meta.id))
    except Exception:
        delete_references(refs)
        raise
    return refs


def delete_references(refs: list[Reference]) -> None:
    for r in refs:
        try:
            client.files.delete(r.file_id)
        except Exception:
            pass


def _ref_blocks(refs: list[Reference]) -> list[dict]:
    if not refs:
        return []
    blocks: list[dict] = [
        {
            "type": "text",
            "text": "Reference standards, guides and codes supplied by the engineer "
            "(use these in preference to memory and cite their clause / table numbers):",
        }
    ]
    blocks += [
        {"type": "document", "source": {"type": "file", "file_id": r.file_id}, "title": r.title}
        for r in refs
    ]
    # Every step resends the references first, so cache them.
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _pdf_block(pdf_bytes: bytes, title: str) -> dict:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode(),
        },
        "title": title,
    }


def _page_blocks(pages: list[PageImage]) -> list[dict]:
    blocks: list[dict] = []
    for p in pages:
        blocks.append(
            {
                "type": "text",
                "text": f"--- Architectural page {p.number} "
                f"(image {p.width}x{p.height}px) ---\nExtracted text:\n{p.text[:4000] or '(none)'}",
            }
        )
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(p.jpeg).decode(),
                },
            }
        )
    # Cache the drawing set: steps 2 and 3 resend the same pages several times.
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


# ---------------------------------------------------------------------------
# Step 1
# ---------------------------------------------------------------------------


def review_geotech(
    geotech_pdf: bytes | None,
    wind_class: str | None,
    site_class: str | None,
    refs: list[Reference] = [],
) -> GeotechReview:
    content: list[dict] = _ref_blocks(refs)
    if geotech_pdf:
        content.append(_pdf_block(geotech_pdf, "Geotechnical report"))
    content.append(
        {
            "type": "text",
            "text": (
                "Step 1 - Review the geotechnical report"
                + ("" if geotech_pdf else " (NONE PROVIDED - set report_provided=false and flag it)")
                + ".\n"
                f"User-provided wind classification: {wind_class or 'not provided'}.\n"
                f"User-provided site classification override: {site_class or 'none'}.\n\n"
                "Extract the AS 2870 site classification, ys, bearing capacity, founding "
                "depth and material, fill, groundwater, tree / abnormal moisture issues and "
                "the report's footing recommendations. If the user gave a wind class use it "
                "(wind_source='user'); otherwise take it from the report if stated. List any "
                "concerns the engineer must resolve."
            ),
        }
    )
    return _call(content, GeotechReview, max_tokens=16000)


# ---------------------------------------------------------------------------
# Step 2
# ---------------------------------------------------------------------------


def assess_design(
    pages: list[PageImage],
    geotech: GeotechReview,
    construction: str,
    extra_notes: str,
    refs: list[Reference] = [],
) -> DesignAssessment:
    content = _ref_blocks(refs) + _page_blocks(pages)
    content.append(
        {
            "type": "text",
            "text": (
                "Step 2 - Review the architectural drawings above.\n\n"
                f"Geotechnical review (step 1):\n{geotech.model_dump_json(indent=2)}\n\n"
                f"Construction material / standard nominated by the engineer: {construction}\n"
                f"Engineer's notes: {extra_notes or 'none'}\n\n"
                "Determine:\n"
                "1. The role of every page (plan, elevation, section, ...).\n"
                "2. Roof type: form, rafters vs trusses, pitch, cladding mass, decks.\n"
                "3. Wall type: external / internal construction and governing standard.\n"
                "4. Load paths: roof -> walls/beams -> floor -> footings, including point "
                "loads, girder trusses, beams and posts.\n"
                "5. Footing type based on the geotechnical review and AS 2870 (state the "
                "clause/figure basis, edge/internal beams, slab thickness and mesh where a "
                "deemed-to-satisfy design applies; flag if engineered design is required, "
                "e.g. class P or E).\n"
                "6. Wall design including bracing to the nominated material standard: "
                "racking forces for the wind class, bracing wall locations/types, tie-down, "
                "lintels, and masonry articulation / control joints.\n"
                "7. Floor framing or slab."
            ),
        }
    )
    return _call(content, DesignAssessment)


# ---------------------------------------------------------------------------
# Step 3
# ---------------------------------------------------------------------------

SHEET_BRIEFS: dict[SheetKind, str] = {
    SheetKind.LOAD_PATHS: (
        "Mark the load paths on the most suitable floor plan: roof load bearing walls, "
        "beams, girder trusses, posts / point loads and where they land on footings. "
        "Use arrows for load direction and rects for bearing walls / beams."
    ),
    SheetKind.ROOF: (
        "Mark the roof framing for rafters only (pitched / skillion rafters, ridge, hip "
        "and valley members, underpurlins, struts) and the deck framing (bearers, joists, "
        "beams, posts). Use the roof plan if there is one, otherwise the floor plan. Do "
        "not detail trusses beyond noting their extent."
    ),
    SheetKind.WALLS: (
        "Mark wall framing on the floor plan: load bearing walls, bracing walls with "
        "type and capacity (racking), lintels over openings, tie-down points, and for "
        "masonry/blockwork the articulation and control joint locations."
    ),
    SheetKind.FOOTINGS: (
        "Mark the footing system on the floor plan per AS 2870 and the geotech report: "
        "edge beams, internal beams / ribs, thickenings, pads under point loads, piers, "
        "step-downs and any setback from trees or drainage."
    ),
    SheetKind.FLOOR: (
        "Mark the floor system: for a slab show slab extents, thickness, mesh, set-downs "
        "and wet areas; for suspended floors show bearers, joists, spans, directions and "
        "supports."
    ),
}


def plan_sheet(
    kind: SheetKind,
    pages: list[PageImage],
    geotech: GeotechReview,
    design: DesignAssessment,
    refs: list[Reference] = [],
) -> MarkupSheet:
    content = _ref_blocks(refs) + _page_blocks(pages)
    content.append(
        {
            "type": "text",
            "text": (
                f"Step 3 - Produce the '{SHEET_TITLES[kind]}' mark-up sheet.\n\n"
                f"{SHEET_BRIEFS[kind]}\n\n"
                f"Geotechnical review:\n{geotech.model_dump_json(indent=2)}\n\n"
                f"Design assessment:\n{design.model_dump_json(indent=2)}\n\n"
                "Choose ONE source page to mark up. Give every mark's coordinates "
                "normalised to that page image (0,0 = top-left, 1,1 = bottom-right) and "
                "place marks on the actual drawn elements - read the image carefully. "
                "Keep labels short; put longer explanation in notes. Use consistent "
                "categories so they can form a legend."
            ),
        }
    )
    return _call(content, MarkupSheet)
