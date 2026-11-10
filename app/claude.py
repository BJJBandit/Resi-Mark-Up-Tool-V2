"""Claude API calls for each review step."""

from __future__ import annotations

import base64
import json
import contextvars
from pathlib import Path
import os
import time
from dataclasses import dataclass
from typing import TypeVar

import anthropic
import httpx2
from pydantic import BaseModel, ValidationError

from . import usage
from .pdf import PageImage
from .rules import BEAM_WIDTH_FROM_MM, BEAM_WIDTH_TO_MM, MIN_STRIP_WIDTH_MM
from .schemas import (
    SHEET_TITLES,
    DesignAssessment,
    GeotechReview,
    MarkupSheet,
    SheetKind,
)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "high")  # step 1 (geotech): engineering judgement
DESIGN_EFFORT = os.environ.get("ANTHROPIC_DESIGN_EFFORT", "medium")  # step 2 (architectural review)
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
- Write everything in British / Australian English (e.g. centres, metres, colour, galvanised,
  fibre, aluminium, kerb, sulphate, vapour, levelling, organisation) - never American spelling.
"""

SYSTEM += (
    f"- FIRM RULE: every strip footing is at least {MIN_STRIP_WIDTH_MM} mm wide, even where "
    "AS 2870 or the geotechnical report would allow narrower. Always state strip footing width "
    f"explicitly (e.g. '{MIN_STRIP_WIDTH_MM}W x <DEPTH> DEEP STRIP FOOTING')."
    f"\n- FIRM RULE: any edge, internal or stiffening beam that AS 2870 gives as "
    f"{BEAM_WIDTH_FROM_MM} mm wide is made {BEAM_WIDTH_TO_MM} mm wide "
    f"(+{BEAM_WIDTH_TO_MM - BEAM_WIDTH_FROM_MM} mm). State beam widths explicitly "
    f"(e.g. '{BEAM_WIDTH_TO_MM}W x <DEPTH> DEEP EDGE BEAM')."
    "\n- FIRM RULE: every lintel and bracing panel is within the building - on a wall of the "
    "building's enclosed rooms. Never place one on a deck, verandah, porch, carport, pergola, "
    "eaves or fence line (use verandah / deck beams there, not lintels)."
)

# Office practice (app/office_rules.md): the office's standard materials, member forms and
# details. Followed in every step unless the engineer's direction for the job says otherwise;
# the FIRM RULES above still win.
OFFICE_RULES = (Path(__file__).parent / "office_rules.md").read_text().strip()
SYSTEM += (
    "\n\nOFFICE PRACTICE - follow these in every step unless the engineer's direction for this "
    "job says otherwise (the FIRM RULES above always apply):\n" + OFFICE_RULES + "\n"
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
# Cache the references and drawing pages for an hour (not 5 minutes): a review's
# steps can be more than 5 minutes apart, and a lapsed cache means re-sending them all.
CACHE_TTL = os.environ.get("ANTHROPIC_CACHE_TTL", "1h")
CACHE = {"type": "ephemeral", "ttl": CACHE_TTL} if CACHE_TTL in ("5m", "1h") else {"type": "ephemeral"}
# Effort is set per message (after the cached content) rather than per request, so
# steps at different efforts still share one cache: a request-level effort change
# would invalidate it. Falls back to request-level effort if the API refuses.
BASE_EFFORT = "medium"
_per_message_effort = os.environ.get("ANTHROPIC_PER_MESSAGE_EFFORT", "1") != "0"
_step = contextvars.ContextVar("step", default="Claude call")
# How the review steps (1, 2, sheets) get JSON back:
#   "local" - the schema is given in the prompt (after the cached content) and the JSON is
#             validated here, retrying on a mismatch. The API's structured-output format is
#             part of the cached prompt, so with it each step (different schema) has its
#             own cache; without it steps 1, 2 and the sheets share one cache of the
#             standards and drawings.
#   "api"   - the API's structured outputs (each step caches separately).
JSON_MODE = os.environ.get("ANTHROPIC_JSON_MODE", "local").strip().lower()
SHARED_CACHE = JSON_MODE == "local"


def _schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()


def _request(messages: list[dict], max_tokens: int, schema: dict | None, effort: str = EFFORT) -> str:
    """One streamed request (avoids HTTP timeouts); returns the text output."""
    global _per_message_effort
    if _per_message_effort:
        try:
            return _send(messages + [{"role": "system", "content": [], "output_config": {"effort": effort}}],
                         max_tokens, schema, BASE_EFFORT, ["mid-conversation-output-config-2026-07-01"])
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if "grammar" in msg or "schema" in msg or not any(
                    k in msg for k in ("per-turn", "effort", "role", "system", "beta")):
                raise
            print(f"[claude] per-message effort refused, using request-level effort: {e}", flush=True)
            _per_message_effort = False
    return _send(messages, max_tokens, schema, effort, [])


def _send(messages: list[dict], max_tokens: int, schema: dict | None, effort: str, betas: list[str]) -> str:
    output_config: dict = {"effort": effort}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        betas=["server-side-fallback-2026-07-01", *betas],
        fallbacks="default",
        # Summarised thinking streams text while Claude works, so the connection
        # never sits silent long enough to time out. Billed the same as omitted.
        thinking={"type": "adaptive", "display": "summarized"},
        output_config=output_config,
        messages=messages,
    ) as stream:
        message = stream.get_final_message()
    usage.record(_step.get(), getattr(message, "model", MODEL) or MODEL, getattr(message, "usage", None))

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


_BUSY_WAITS = (30, 60, 120)  # s: waits after a rate-limit / overloaded / server error


def _request_retrying(messages: list[dict], max_tokens: int, schema: dict | None, effort: str) -> str:
    """_request, waiting and retrying when the API is busy: rate limited (429, e.g. several
    sheets at once on a low usage tier), overloaded (529) or a server error (5xx). The SDK's
    own quick retries often aren't long enough for a per-minute limit to reset."""
    for wait in (*_BUSY_WAITS, None):
        try:
            return _request_once(messages, max_tokens, schema, effort)
        except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
            if wait is None:
                raise ClaudeError(f"the Claude API was busy and refused the request 4 times ({e})") from e
            print(f"[claude] {_step.get()}: API busy ({type(e).__name__}), retrying in {wait}s", flush=True)
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code not in (429, 503, 529) or wait is None:
                raise
            print(f"[claude] {_step.get()}: API busy ({e.status_code}), retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _request_once(messages: list[dict], max_tokens: int, schema: dict | None, effort: str) -> str:
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


def _call(content: list[dict], out: type[T], step: str, max_tokens: int = MAX_TOKENS, effort: str = EFFORT,
          local_json: bool = False) -> T:
    """Request a JSON answer matching `out`: the API's structured outputs, or (local_json)
    the schema in the prompt validated here. If the API rejects the schema (e.g. compiled
    grammar too large) it also falls back to local validation. Errors name the step."""
    _step.set(step)
    try:
        return _call_inner(content, out, max_tokens, effort, local_json)
    except ClaudeError as e:
        raise ClaudeError(f"{step}: {e}") from e


def _call_inner(content: list[dict], out: type[T], max_tokens: int, effort: str, local_json: bool = False) -> T:
    schema = _schema(out)
    if not local_json:
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
    last_error: Exception | None = None
    for _ in range(3):
        # Each try is a fresh request (same cached prefix) with the last error appended:
        # replaying a model turn without its thinking blocks can be rejected.
        note = [] if last_error is None else [{"type": "text", "text": (
            "Your previous answer was rejected because the JSON was invalid: "
            f"{str(last_error)[:1500]}\nAnswer again with a complete, corrected JSON object only.")}]
        messages = [{"role": "user", "content": [*content, {"type": "text", "text": instruction}, *note]}]
        text = _request_retrying(messages, max_tokens, None, effort)
        try:
            return out.model_validate_json(_extract_json(text))
        except (ValueError, ValidationError) as err:
            last_error = err
    raise ClaudeError(f"Claude's JSON did not match the expected structure: {last_error}")


# ---------------------------------------------------------------------------
# Reference documents (standards, guides, codes) supplied by the engineer
# ---------------------------------------------------------------------------


@dataclass
class Reference:
    title: str
    file_id: str = ""  # PDF pages uploaded via the Files API, or
    text: str = ""  # stored text extract of the pages (app/refstore.py)


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
        if not r.file_id:
            continue
        try:
            client.files.delete(r.file_id)
        except Exception:
            pass


def _ref_blocks(refs: list[Reference], cache: bool = True) -> list[dict]:
    if not refs:
        return []
    blocks: list[dict] = [
        {
            "type": "text",
            "text": "Reference standards, guides and codes supplied by the engineer "
            "(use these in preference to memory and cite their clause / table numbers). "
            "Where a title says 'pages X of Y', it is an extract of those pages of the "
            "original document. Some are text extracts transcribed from the original pages "
            "(marked by PDF page); treat them as the document itself:",
        }
    ]
    blocks += [
        {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": r.text},
         "title": r.title}
        if r.text else
        {"type": "document", "source": {"type": "file", "file_id": r.file_id}, "title": r.title}
        for r in refs
    ]
    # The sheets resend the references first, so cache them there.
    if cache:
        blocks[-1]["cache_control"] = dict(CACHE)
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


def _page_blocks(pages: list[PageImage], cache: bool = True) -> list[dict]:
    blocks: list[dict] = []
    for p in pages:
        blocks.append(
            {
                "type": "text",
                "text": f"--- Architectural page {p.number}" + (f" ({p.label})" if p.label else "") + " "
                f"(image {p.width}x{p.height}px) ---\nExtracted text:\n{p.text[:4000] or '(none)'}"
                + (f"\nDrawing scale: {p.scale_note}" if p.scale_note else "")
                + (f"\nIf this is a floor plan, measured from its walls: {p.outline_note}" if p.outline_note else "")
                + (f"\nWritten dimensions / levels with position on this image (x,y 0-1; 'v' = written "
                   f"vertically):\n{p.dims}" if p.dims else ""),
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
    # Cache the drawing set: every sheet resends the same pages.
    if blocks and cache:
        blocks[-1]["cache_control"] = dict(CACHE)
    return blocks


# ---------------------------------------------------------------------------
# Step 1
# ---------------------------------------------------------------------------


def review_geotech(
    geotech_pdf: bytes | None | list[tuple[str, bytes]],
    wind_class: str | None,
    site_class: str | None,
    refs: list[Reference] = [],
    direction: str = "",
) -> GeotechReview:
    # With the API's answer formats (ANTHROPIC_JSON_MODE=api) the next step can't reuse a
    # cache written here, so only write one when the steps share a cache.
    # (measured) stops it reusing this step's cache, so a write would be wasted.
    content: list[dict] = _ref_blocks(refs, cache=SHARED_CACHE)
    docs = geotech_pdf if isinstance(geotech_pdf, list) else ([("Geotechnical report", geotech_pdf)] if geotech_pdf else [])
    for title, data in docs:
        content.append(_pdf_block(data, title))
    if len(docs) > 1:
        content.append({"type": "text", "text": f"{len(docs)} geotechnical documents are attached (e.g. report, "
                        "addendum, borehole logs); review them together and say which one each finding is from."})
    content.append(
        {
            "type": "text",
            "text": (
                "Step 1 - Review the geotechnical report"
                + ("" if docs else " (NONE PROVIDED - set report_provided=false and flag it)")
                + ".\n"
                f"User-provided wind classification: {wind_class or 'not provided'}.\n"
                f"User-provided site classification override: {site_class or 'none'}.\n"
                f"Engineer's direction: {direction or 'none'}\n"
                "Follow the direction where it applies to this step (it overrides defaults but "
                "not the FIRM RULES), but still record what the report itself says; where the "
                "direction differs from the report, add a concern stating both.\n\n"
                "Extract the AS 2870 site classification, ys, bearing capacity, founding "
                "depth and material, fill, groundwater, tree / abnormal moisture issues and "
                "the report's footing recommendations. If the user gave a wind class use it "
                "(wind_source='user'); otherwise take it from the report if stated. List any "
                "concerns the engineer must resolve."
            ),
        }
    )
    return _call(content, GeotechReview, "Step 1 (geotech review)", local_json=SHARED_CACHE)


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
    # As in review_geotech: cache only when the steps share a cache.
    content = _ref_blocks(refs, cache=SHARED_CACHE) + _page_blocks(pages, cache=SHARED_CACHE)
    content.append(
        {
            "type": "text",
            "text": (
                "Step 2 - Review the architectural drawings above.\n\n"
                f"Geotechnical review (step 1):\n{geotech.model_dump_json(indent=2)}\n\n"
                f"Construction material / standard nominated by the engineer: {construction}\n"
                f"Engineer's direction (follow it; it overrides defaults but not the FIRM RULES): "
                f"{extra_notes or 'none'}\n\n"
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
                "   Bracing calculation (timber framing, AS 1684.2 Section 8) in racking and "
                "bracing_capacities: for each wind direction (each storey), the area of elevation to "
                "Cl 8.3.3 / Fig 8.2 (from half the storey's wall height up to the roof, roof and gables "
                "included) with its working, the pressure from Tables 8.1-8.5 for the wind class, building "
                "width, roof pitch and roof form, and force = area x pressure; state which walls on the plan "
                "resist it (they run parallel to the wind). Give the Table 8.18 capacity (kN/m) of every "
                "bracing type you will use, with the same labels as the legend (e.g. h(b), g, d). Use the "
                "supplied standard's values; if it isn't supplied, say so in the reference.\n"
                "7. Floor framing or slab.\n"
                "8. Cross-reference the plan views with the elevation views for every window / door: "
                "match each elevation to its wall, and check each opening's code, size and position "
                "(order along the wall, distance from corners) agree. List every discrepancy in "
                "opening_crosscheck; leave it empty only if they all agree.\n"
                "9. Check that the PROPOSED plan view dimensions match the PROPOSED elevation "
                "dimensions (ignore existing / demolished work on alteration jobs). Match each elevation "
                "to its wall, then compare: overall building lengths and depths, wall lengths and steps "
                "in the walls, wall / ceiling heights and floor levels, eaves overhangs, and the extent of "
                "every closed / covered roof area (alfresco, porch, verandah, carport, deck roof) - its "
                "length and width on the plan (or roof plan) against its extent on the elevations, and "
                "whether it is shown as roofed in both. Read the written dimensions; where a dimension "
                "is not written, scale it from the drawing (the plan's scale) and say so in the note. "
                "Record each comparison in dimension_crosscheck with status match / mismatch / not "
                "shown; include every mismatch.\n"
                "10. calculations: the calculation sheet entries for the key decisions (roof load widths, "
                "lintels / beams, bearers / joists / rafters, columns, footing and slab, tie-down) - basis "
                "(standard, clause / table, inputs), working and result, as an engineer's calc sheet. Not "
                "the racking / bracing check: the app works that out from the panels on the wall plan.\n"
                "11. List in information_requests any standard, project document or information that was "
                "NOT supplied and would materially change the design (e.g. a standard / table you need "
                "but don't have, the truss layout with girder positions, a BAL report, survey levels). "
                "For each give why it matters and the assumption you will use if it isn't available - "
                "and use that assumption in this assessment meanwhile. The engineer is asked about each "
                "before the sheets are drawn, so list only what matters (max 5); don't ask for anything "
                "already supplied, covered by the office practice, or that the engineer's direction says "
                "is not available."
            ),
        }
    )
    return _call(content, DesignAssessment, "Step 2 (architectural review)", effort=DESIGN_EFFORT,
                 local_json=SHARED_CACHE)


# ---------------------------------------------------------------------------
# Step 3
# ---------------------------------------------------------------------------

HOUSE_STYLE = """House style for mark-ups. This sets the PRESENTATION only - the format, tags and
layout of the engineer's sheets. It contains no engineering values: every size, grade, spacing,
reinforcement, fixing and note must come from THIS job (its drawings, geotech report, wind and
site classification, the design assessment and the standards / guides supplied). Never reuse a
specification just because it looks typical.
- Member layout plan only: tag every structural member and describe it in the schedule.
- Tags: B = bearer, J = joist, LD = deck ledger, L = lintel / ledger, VB = verandah beam,
  R = rafter, RB = ridge beam, UP = underpurlin, C = column, DS = double stud,
  EB = edge beam, IB = internal beam, SF = strip footing, P = pier / pad,
  CJ / AJ = control / articulation joint, LP = load path step. Number them B1, B2 ...
- Lay members out to scale using the page's drawing scale (given with each page): spacing and
  lengths on the plan should match the real ones.
  - Bearers: draw EVERY bearer line, parallel, at its scheduled spacing across the whole floor
    (e.g. bearers at 2200 ctrs are never drawn more than 2200 mm apart), each from pier / wall
    to pier / wall.
  - Joists and rafters: one representative 'member' drawn along a typical joist / rafter
    from support to support (its span), plus a 'span' double arrow drawn
    PERPENDICULAR to that member - across the members, parallel to their supports - covering
    the full width of the area where that member repeats at its spacing, from wall to wall
    and never past a wall. Tag both the same (J1, R1 ...). Do not draw every joist or rafter.
    Floor joists: the representative joist runs wall to wall, continuous over the bearers.
  - Trusses (by the truss manufacturer): one 'span' arrow ALONG the truss span, tagged T,
    showing the assumed truss span direction.
  - Schedule bearers, joists and rafters with their spacing: '... AT <SPACING> CTRS'.
- EVERY 'member' carries its schedule tag (EB1, SF1, IB1, B1, L1 ...) - never leave a tag empty;
  draw each footing / beam run as its own tagged member. Clouds are only for items the engineer
  must check, never to outline a slab or an area.
- Draw each member once along its real position: 'member' from one end to the other,
  'span' double arrows across (perpendicular to) joists / rafters to show where they repeat,
  'column' at each post, 'pier' at each pier, 'bracing' along each bracing panel tagged
  '<bracing type> - <panel length mm>' (type d, diagonal strap, is drawn dashed).
- Callouts carry the construction notes for the element they point at, in the form
  '<WHAT> <SIZE / SPEC> <WHERE / SPACING>. <FIXING OR CONDITION>. TYPICAL.' Point each
  callout's start at the element it describes. Use 'TYPICAL.' instead of repeating a callout.
  Keep to about 4-12 callouts per sheet.
- Be economical: one element per real member (no duplicates), tag repeated items once and
  rely on 'TYPICAL.', and aim for under ~150 elements per sheet.
- Schedule items: tag + UPPERCASE '<NUMBER/><DEPTH>x<BREADTH> <GRADE> <MEMBER> [AT <SPACING> CTRS]'
  for timber, '<SECTION> <MEMBER>' for steel - sized for this job. ONLY the member type and
  spacing (for footings / slabs: size and reinforcement) - no locations, spans, roof load
  widths, fixings, tie-downs, reasons or notes in the schedule; those go in callouts / notes.
- Notes: UPPERCASE, each starting with '-': the general notes this sheet needs for this job.
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
        "ROOF FRAMING PLAN - rafters only (and the deck / verandah roof). Rafters R 'span' (across the rafters) "
        "arrows plus a 'member' on a representative rafter, ridge / hip / valley members, "
        "underpurlins, verandah beams VB, columns C, double studs DS at beam supports. "
        "If the main roof is trusses, note that they are by the truss manufacturer and show "
        "the assumed truss span direction with a span arrow - do not detail trusses. "
        "Schedule = 'ROOF FRAMING SCHEDULE:'. Classification: AS4055 wind class."
    ),
    SheetKind.WALLS: (
        "WALL FRAMING PLAN. Lintels L1, L2 ... as 'member' over each opening in a load-bearing "
        "wall, double studs DS, columns C, and every bracing panel as 'bracing' (type and length "
        "from the bracing standard for this job's racking force and wind class). Provide enough "
        "bracing in EACH direction that the sum of panel length x capacity (bracing_capacities in the "
        "design assessment) is at least that direction's racking force (racking) - the app checks this "
        "on the calculation sheet. "
        "Do NOT show articulation or control joints here - they go on the separate articulation "
        "joint sheet drawn on the elevations. "
        "LINTELS: every window and door in a load-bearing or external wall gets a lintel - including "
        "the entry door, sliding doors and sidelights. Architect's codes give height then width in "
        "hundreds of mm (e.g. a code 'A1812' is 1800 high x 1200 wide); use the width for the "
        "span. Size each lintel for its span and roof load width; on the same wall a wider opening "
        "never gets a smaller lintel than a narrower one. "
        "Schedule = 'LINTEL SCHEDULE:' (plus columns). Legend = 'BRACING LEGEND:' with one item "
        "per bracing type used: tag '<type>': '<BRACING TYPE DESCRIPTION> TO <STANDARD>'. Notes "
        "include the tie-down this job needs. Classification: AS4055 wind class."
    ),
    SheetKind.FOUNDATION: (
        "FOOTING & FLOOR PLAN - footings AND the floor framing / slab on ONE sheet. "
        "FOOTINGS to AS 2870 and the geotech report: edge beams / strip footings EB / SF "
        "as 'member' around the perimeter, internal beams IB, slab thickenings, piers "
        "'pier' (corners and max spacing), pads under point loads, step-downs. Strip footings "
        f"are never narrower than {MIN_STRIP_WIDTH_MM} mm, and edge / internal beams that AS 2870 "
        f"gives as {BEAM_WIDTH_FROM_MM} wide are {BEAM_WIDTH_TO_MM} wide. "
        "FLOOR on the same plan: suspended floors - bearers B as 'member' (on or beside the "
        "footing / pier lines that carry them), joists J as 'span' arrows (plus a 'member' on a "
        "representative joist), deck ledgers LD, deck bearers and joists, columns C; slab on "
        "ground - draw the slab as ONE 'member' tagged S1 running diagonally from one corner of the slab to the opposite corner (no cloud), thickenings / set-downs / wet areas. "
        "Callouts give each footing's size and reinforcement and the slab thickness, "
        "reinforcement and concrete (or floor framing fixings) as determined for this job. "
        "Keep the combined sheet readable: tag repeated items once and use 'TYPICAL.'. "
        "Schedule = 'FOOTING & FLOOR SCHEDULE:' (footings, floor members and columns). Notes: "
        "the footing, slab / floor, site preparation and drainage notes this site needs. "
        "Classification: 'AS2870 SITE CLASSIFICATION: <class>'."
    ),
    SheetKind.FOOTINGS: (
        "FOOTING PLAN to AS 2870 and the geotech report. Edge beams / strip footings EB / SF "
        "as 'member' around the perimeter, internal beams IB, slab thickenings, piers "
        "'pier' (corners and max spacing), pads under point loads, step-downs. Strip footings "
        f"are never narrower than {MIN_STRIP_WIDTH_MM} mm, and edge / internal beams that AS 2870 "
        f"gives as {BEAM_WIDTH_FROM_MM} wide are {BEAM_WIDTH_TO_MM} wide. Callouts give each footing's size and "
        "reinforcement as determined for this job. Schedule = 'FOOTING SCHEDULE:'. Notes: the "
        "footing, site preparation and drainage notes this site needs. Classification: "
        "'AS2870 SITE CLASSIFICATION: <class>'."
    ),
    SheetKind.FLOOR: (
        "FLOOR FRAMING PLAN. Suspended floors: bearers B as 'member', joists J as 'span' "
        "arrows (plus a 'member' on a representative joist), deck ledgers LD, deck bearers and "
        "joists, columns C, piers under bearers. Slab on ground: the slab as ONE 'member' tagged S1 "
        "running diagonally corner to corner of the slab (no cloud), "
        "thickenings / set-downs / wet areas with callouts giving this job's slab thickness, "
        "reinforcement and concrete as determined. Schedule = 'FLOOR FRAMING SCHEDULE:' "
        "(include columns). Classification: 'AS2870 SITE CLASSIFICATION: <class>'."
    ),
    SheetKind.ARTICULATION: (
        "ARTICULATION JOINT ELEVATIONS - masonry only. Use the ELEVATION page (not a plan): choose "
        "the page with the elevations and set plan_region around all of them (exclude the title "
        "block). Draw each vertical articulation joint (AJ) - or control joint (CJ) for blockwork - "
        "as a 'member' VERTICAL line on the elevation, from the top of the masonry down to the "
        "footing / ground line, at the positions the masonry standard and this site class require "
        "(maximum spacings, near corners and changes of height, beside openings). A joint NEVER "
        "passes through a window or door: put it at the jamb (side) of the opening. Cross-reference "
        "the plan for wall lengths, corners and openings so each joint is at the right place on its "
        "wall. Callouts give the joint width / sealant / ties for this job. Schedule = "
        "'ARTICULATION JOINT SCHEDULE:'. Classification: 'AS2870 SITE CLASSIFICATION: <class>'."
    ),
}


def plan_sheet(
    kind: SheetKind,
    pages: list[PageImage],
    geotech: GeotechReview,
    design: DesignAssessment,
    refs: list[Reference] = [],
    direction: str = "",
) -> MarkupSheet:
    content = _ref_blocks(refs) + _page_blocks(pages)
    # The same for every sheet: cached after the first sheet so the others read it cheaply.
    content.append(
        {
            "type": "text",
            "text": (
                f"{HOUSE_STYLE}\n\n"
                f"Geotechnical review:\n{geotech.model_dump_json(indent=2)}\n\n"
                f"Design assessment:\n{design.model_dump_json(indent=2)}"
            ),
            "cache_control": dict(CACHE),
        }
    )
    content.append(
        {
            "type": "text",
            "text": (
                f"Step 3 - Produce the '{SHEET_TITLES[kind]}' mark-up sheet.\n\n"
                f"{SHEET_BRIEFS[kind]}\n\n"
                f"Engineer's direction (follow it on this sheet where it applies; it overrides "
                f"defaults but not the FIRM RULES): {direction or 'none'}\n\n"
                "Choose ONE plan page to mark up and give plan_region tightly around the plan "
                "drawing. Give every element's coordinates normalised to that WHOLE page image "
                "(0,0 = top-left, 1,1 = bottom-right) and put them on the actual drawn walls / "
                "members - read the image carefully. Every tag used on the plan must appear in "
                "the schedule, and every schedule item must appear on the plan."
            ),
        }
    )
    return _call(content, MarkupSheet, f"Step 3 ({SHEET_TITLES[kind]})", effort=SHEET_EFFORT,
                 local_json=SHARED_CACHE)


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


REF_EXTRACT_EFFORT = os.environ.get("REF_EXTRACT_EFFORT", "low")  # transcription, not judgement


def transcribe_reference_pages(stem: str, pdf: bytes, pages: list[int]) -> dict[int, str]:
    """Transcribe pages of a standard / guide to text once, for the reference store.
    pdf holds just these pages, in order."""
    from .schemas import RefTranscript

    mapping = ", ".join(f"page {i + 1} of the attachment = PDF page {p}" for i, p in enumerate(pages))
    text = (
        f"The attached PDF holds pages of '{stem}' ({mapping}).\n\n"
        "Transcribe each page completely and exactly, for an engineer who will design from your text "
        "instead of the page:\n"
        "- every clause and sub-clause with its number and heading, word for word (no summarising);\n"
        "- every table in full as a markdown table: its number and title, every column heading and "
        "unit, every row and every value exactly as printed (keep blank / '-' / 'NA' cells), and its notes;\n"
        "- equations, notes, footnotes and cross-references as printed;\n"
        "- each figure: its number and title, and what it shows in words, with every dimension, value "
        "and label on it;\n"
        "- the printed page number if shown.\n"
        "Skip only running headers / footers and copyright lines. Write [illegible] for anything you "
        "can't read - never guess a value. Return one entry per page with pdf_page as given above."
    )
    result = _call([_pdf_block(pdf, stem), {"type": "text", "text": text}], RefTranscript,
                   f"Extracting {stem}", effort=REF_EXTRACT_EFFORT)
    return {p.pdf_page: p.text for p in result.pages if p.pdf_page in pages}


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
