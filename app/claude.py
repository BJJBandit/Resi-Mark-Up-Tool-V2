"""Claude API calls for each review step."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import TypeVar

import anthropic
import httpx2
from pydantic import BaseModel, ValidationError

from .pdf import PageImage
from .rules import MIN_STRIP_WIDTH_MM
from .schemas import (
    SHEET_TITLES,
    DesignAssessment,
    GeotechReview,
    MarkupSheet,
    SheetKind,
)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "high")  # steps 1-2: engineering judgement
SHEET_EFFORT = os.environ.get("ANTHROPIC_SHEET_EFFORT", "medium")  # step 3: placing mark-ups

# Long thinking can leave a stream quiet for a while; allow 30 min between bytes
# (the SDK default is 10) and handle the timeout ourselves below.
TIMEOUT_S = float(os.environ.get("ANTHROPIC_TIMEOUT_S", "1800"))
client = anthropic.Anthropic(timeout=httpx2.Timeout(TIMEOUT_S, connect=15.0))

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
- Use metric units (mm, kPa, kN).
"""

SYSTEM += (
    f"- FIRM RULE: every strip footing is at least {MIN_STRIP_WIDTH_MM} mm wide, even where "
    "AS 2870 or the geotechnical report would allow narrower. Always state strip footing width "
    "explicitly (e.g. '450W x MIN 450 DEEP STRIP FOOTING')."
)


class ClaudeError(RuntimeError):
    pass


class CutOff(ClaudeError):
    """The response hit max_tokens (thinking counts toward the limit)."""


# Network-level failures mid-stream are not retried by the SDK.
_TRANSIENT = (anthropic.APITimeoutError, anthropic.APIConnectionError, httpx2.TimeoutException, httpx2.TransportError)


# Opus-class models think before answering and the thinking counts toward
# max_tokens, so leave plenty of room; retry once at the model's maximum.
MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "64000"))
MAX_TOKENS_RETRY = 128000


def _schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()


def _request(messages: list[dict], max_tokens: int, schema: dict | None, effort: str = EFFORT) -> str:
    """One streamed request (avoids HTTP timeouts); returns the text output."""
    output_config: dict = {"effort": effort}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        # Summarised thinking streams text while Claude works, so the connection
        # never sits silent long enough to time out. Billed the same as omitted.
        thinking={"type": "adaptive", "display": "summarized"},
        output_config=output_config,
        messages=messages,
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        detail = getattr(message, "stop_details", None)
        raise ClaudeError(f"Claude declined the request: {detail}")
    if message.stop_reason == "max_tokens":
        raise CutOff(f"Claude's response was cut off at {max_tokens} tokens.")
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    if not text:
        raise ClaudeError("Claude returned no text content.")
    return text


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in response")
    return text[start : end + 1]


def _request_retrying(messages: list[dict], max_tokens: int, schema: dict | None, effort: str) -> str:
    """Retry once on a cut-off (at the model maximum) or a dropped / timed-out stream."""
    try:
        return _request(messages, max_tokens, schema, effort)
    except CutOff:
        if max_tokens >= MAX_TOKENS_RETRY:
            raise
        return _request(messages, MAX_TOKENS_RETRY, schema, effort)
    except _TRANSIENT:
        try:
            return _request(messages, max_tokens, schema, effort)
        except _TRANSIENT as e:
            raise ClaudeError(f"the connection to Claude timed out or dropped twice ({type(e).__name__}).") from e


def _call(content: list[dict], out: type[T], step: str, max_tokens: int = MAX_TOKENS, effort: str = EFFORT) -> T:
    """Structured-output request. If the API rejects the schema (e.g. compiled
    grammar too large), fall back to plain JSON validated locally. Errors name the step."""
    try:
        return _call_inner(content, out, max_tokens, effort)
    except ClaudeError as e:
        raise ClaudeError(f"{step}: {e}") from e


def _call_inner(content: list[dict], out: type[T], max_tokens: int, effort: str) -> T:
    schema = _schema(out)
    messages = [{"role": "user", "content": content}]
    try:
        return out.model_validate_json(_request_retrying(messages, max_tokens, schema, effort))
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
        text = _request_retrying(messages, max_tokens, None, effort)
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
            "(use these in preference to memory and cite their clause / table numbers). "
            "Where a title says 'pages X of Y', it is an extract of those pages of the "
            "original document:",
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
    return _call(content, GeotechReview, "Step 1 (geotech review)")


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
    return _call(content, DesignAssessment, "Step 2 (architectural review)")


# ---------------------------------------------------------------------------
# Step 3
# ---------------------------------------------------------------------------

HOUSE_STYLE = """House style for mark-ups (follow it closely - it matches the engineer's own sheets):
- Member layout plan only: tag every structural member and describe it in the schedule.
- Tags: B = bearer, J = joist, LD = deck ledger, L = lintel / ledger, VB = verandah beam,
  R = rafter, RB = ridge beam, UP = underpurlin, C = column, DS = double stud,
  EB = edge beam, IB = internal beam, SF = strip footing, P = pier / pad,
  CJ / AJ = control / articulation joint, LP = load path step. Number them B1, B2 ...
- Draw each member once along its real position: 'member' from one end to the other,
  'span' double arrows across joists / rafters / trusses to show span direction,
  'column' at each post, 'pier' at each bored pier (bearers: at max spacing along
  the bearer and at corners), 'bracing' along each bracing panel tagged with type
  and length, e.g. 'h(b) - 900', 'g - 600', 'd - 2400' (d = diagonal strap, dashed).
- Callouts carry the construction notes, written like:
  '450 DIAM BORED PIER TO ROCK AT CORNERS AND MAX 3000 CTRS UNDER STRIP FOOTING. 1/N16 CENTRAL. TYPICAL.'
  '6 CFW 8 PLATE CLEAT TO C2. CLEAT CONNECT VB1 TO C2 WITH 3/M10 BOLTS. MIN 50 BETWEEN BOLTS AND
  BETWEEN BOLTS AND TIMBER END. TYPICAL.' Point each callout's start at the element it describes.
  Use 'TYPICAL.' instead of repeating a callout. Keep to about 4-12 callouts per sheet.
- Be economical: one element per real member (no duplicates), tag repeated items once and
  rely on 'TYPICAL.', and aim for under ~150 elements per sheet.
- Schedule items: tag + UPPERCASE size/grade/spacing, e.g. 'B1': '2/240x45 F7 TP CONTINUOUS BEARER',
  'J1': '140x45 F7 TP JOISTS AT 450 CTRS', 'L2': '120x35 F17/LVL LINTEL', 'C1': '89x3.5 SHS COLUMN'.
- Notes: UPPERCASE, each starting with '-', e.g. '-THIS DRAWING IS A MEMBER LAYOUT PLAN ONLY. BUILDER TO
  CONSULT ARCHITECTURAL DRAWINGS FOR DIMENSIONS, SECTIONS AND CONNECTION CONFIGURATION.',
  '-ALL TIMBER FRAMING TO BE IN ACCORDANCE WITH AS 1684.2 2021 AND AS 1720.1 2010.'
- All text UPPERCASE. Australian spelling and units (mm, kPa, kN)."""

SHEET_BRIEFS: dict[SheetKind, str] = {
    SheetKind.LOAD_PATHS: (
        "LOAD PATH PLAN. Show how load gets from the roof to the ground: 'span' arrows for "
        "load direction (truss / rafter spans), 'member' lines (tags LP1, LP2 ...) along "
        "load-bearing walls, beams and girder lines, 'column' at posts and point loads, "
        "'pier' where loads land on footings. Schedule = 'LOAD PATH SCHEDULE:' describing "
        "each LP step (what it carries and where it goes). Classification: AS4055 wind class."
    ),
    SheetKind.ROOF: (
        "ROOF FRAMING PLAN - rafters only (and the deck / verandah roof). Rafters R1 'span' "
        "arrows plus a 'member' on a representative rafter, ridge / hip / valley members, "
        "underpurlins, verandah beams VB, columns C, double studs DS at beam supports. "
        "If the main roof is trusses, add a callout 'TRUSSES TO MANUFACTURER'S "
        "SPECIFICATIONS' and a span arrow 'ASSUMED TRUSS SPAN DIRECTION' - do not detail "
        "trusses. Schedule = 'ROOF FRAMING SCHEDULE:'. Classification: AS4055 wind class."
    ),
    SheetKind.WALLS: (
        "WALL FRAMING PLAN. Lintels L1, L2 ... as 'member' over each opening in a load-bearing "
        "wall, double studs DS, columns C, and every bracing panel as 'bracing' (type and length "
        "from AS 1684.2 Table 8.18, enough to resist the racking force for the wind class). "
        "For masonry / blockwork show control and articulation joints as 'member' CJ1 / AJ1. "
        "Schedule = 'LINTEL SCHEDULE:' (plus columns). Legend = 'BRACING LEGEND:' with items "
        "like tag 'h(b)': 'TYPE h(B) PLY BRACING TO AS 1684.2 2021'. Notes include tie-down "
        "(e.g. '-TYPICAL TIE DOWN TO SIDES OF LINTELS 30x0.8 G.I. STRAP WITH 6/30x2.8 DIAMETER "
        "NAILS EACH END OF STRAP AND M10 BOLTS TO FLOOR ...'). Classification: AS4055 wind class."
    ),
    SheetKind.FOOTINGS: (
        "FOOTING PLAN to AS 2870 and the geotech report. Edge beams / strip footings EB / SF "
        "as 'member' around the perimeter, internal beams IB, slab thickenings, bored piers "
        "'pier' (corners and max spacing), pads under point loads, step-downs. Strip footings "
        f"are never narrower than {MIN_STRIP_WIDTH_MM} mm. Callouts give "
        "sizes and reinforcement, e.g. '450W x MIN 450 DEEP STRIP FOOTING. 4/L11TM TOP AND "
        "BOTTOM WITH 50 COVER.'. Schedule = 'FOOTING SCHEDULE:'. Notes include site prep / "
        "fill to AS2870 and AS3798, step footings to AS2870 5.4.3, articulate masonry to "
        "AS4773, drainage falls, and the CSIRO BTF 18 notice to owner. Classification: "
        "'AS2870 2011 SITE CLASSIFICATION: <class>'."
    ),
    SheetKind.FLOOR: (
        "FLOOR FRAMING PLAN. Suspended floors: bearers B as 'member', joists J as 'span' "
        "arrows (plus a 'member' on a representative joist), deck ledgers LD, deck bearers and "
        "joists, columns C, piers under bearers. Slab on ground: slab extent as a 'cloud', "
        "thickenings / set-downs / wet areas with callouts (e.g. '100 THICK REINFORCED CONCRETE "
        "SLAB. SL82 TOP WITH 30 COVER. 25 MPA GRADE.'). Schedule = 'FLOOR FRAMING SCHEDULE:' "
        "(include columns). Classification: 'AS2870 2011 SITE CLASSIFICATION: <class>'."
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
                f"{SHEET_BRIEFS[kind]}\n\n{HOUSE_STYLE}\n\n"
                f"Geotechnical review:\n{geotech.model_dump_json(indent=2)}\n\n"
                f"Design assessment:\n{design.model_dump_json(indent=2)}\n\n"
                "Choose ONE plan page to mark up and give plan_region tightly around the plan "
                "drawing. Give every element's coordinates normalised to that WHOLE page image "
                "(0,0 = top-left, 1,1 = bottom-right) and put them on the actual drawn walls / "
                "members - read the image carefully. Every tag used on the plan must appear in "
                "the schedule, and every schedule item must appear on the plan."
            ),
        }
    )
    return _call(content, MarkupSheet, f"Step 3 ({SHEET_TITLES[kind]})", effort=SHEET_EFFORT)


# ---------------------------------------------------------------------------
# Reference documents: choose the pages the review needs from the contents
# ---------------------------------------------------------------------------

REVIEW_TOPICS = """This review needs, for a residential (Class 1/10) building:
- site classification and footing / slab design (AS 2870: classification, deemed-to-satisfy
  footing and slab details, edge and internal beams, strip footings, piers, articulation)
- wind classification and its use (AS 4055) where the document covers it
- wall framing: racking forces, bracing types and capacities, bracing layout, nominal bracing
- tie-down / fixings and connections for the wind class
- span tables for lintels, beams, bearers, joists, rafters, ridge / hip / valley members,
  underpurlins, verandah beams and posts (and the matching supplements / span tables)
- masonry: articulation and control joints, lintels, veneer ties (AS 4773 / AS 3700)
- floor framing / slab details relevant to the drawings"""


def select_reference_pages(doc, bookmarks: str, contents: str, pmap: str, context: str, budget: int) -> list[dict]:
    """Ask Claude which PDF pages of a standard / guide this job needs."""
    from .refpages import parse_selection
    from .schemas import RefSelection

    text = (
        f"Document: {doc.stem} ({doc.page_count} pages)\n\n"
        f"Job context:\n{context}\n\n{REVIEW_TOPICS}\n\n"
        + (f"Bookmarks (title -> PDF page):\n{bookmarks}\n\n" if bookmarks else "")
        + (f"Contents pages (printed page numbers):\n{contents}\n\n" if contents else "")
        + "Page map - first lines of every PDF page (pNN = PDF page number; printed page numbers "
        "and section headings appear in the text, so use this to convert contents entries to PDF pages):\n"
        f"{pmap}\n\n"
        "Choose ONLY the sections, clauses and tables of this document that this job's review needs. "
        "Return PDF page numbers (the pNN numbers), not printed page numbers. Include a table's "
        "continuation pages and any notes pages it depends on. Skip front matter, commentary, "
        f"appendices and topics this job does not need. Use at most {budget} pages in total - "
        "usually far fewer."
    )
    result = _call([{"type": "text", "text": text}], RefSelection,
                   f"Selecting pages from {doc.stem}", max_tokens=16000, effort="low")
    return [{"title": s.title, "why": s.why, "pages": parse_selection(s.pdf_pages, doc.page_count)}
            for s in result.sections]
