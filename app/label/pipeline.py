"""Run the labelling stage over an extracted Project."""

from __future__ import annotations

import logging

import pymupdf

from ..model import Project
from .geotech import parse_geotech
from .llm import UsageTotals, api_available
from .roof_text import apply_roof_text, scan_roof_text
from .walls import apply_storey_labels, label_storey

log = logging.getLogger(__name__)

ROOF_CONTEXT_KINDS = {"roof_plan", "section", "elevation"}


def _page_texts(pdf_bytes: bytes, pages: list[int]) -> dict[int, str]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    return {p: doc[p - 1].get_text("text") for p in pages if 1 <= p <= doc.page_count}


def label_project(
    project: Project,
    arch_pdf: bytes,
    geotech_pdf: bytes | None,
    totals: UsageTotals | None = None,
    use_claude: bool = True,
) -> UsageTotals:
    """Fill the judgement fields of `project` in place.

    Order matters for tokens: deterministic text facts first, then one Claude call per
    storey, then one for the geotech report. With `use_claude=False` (or no API key) only
    the deterministic part runs and the gaps are listed in `review.needs_attention`.
    """
    totals = totals or UsageTotals()
    attention = project.review.needs_attention

    # 1. Roof facts from text, for every building (elevation/section pages usually name the unit).
    ctx_pages = [p.number for p in project.pages if p.kind in ROOF_CONTEXT_KINDS]
    texts = _page_texts(arch_pdf, ctx_pages or [p.number for p in project.pages])
    rt = scan_roof_text(texts)
    for b in project.buildings:
        set_ = apply_roof_text(b.roof, rt)
        if set_:
            log.info("roof text %s: %s", b.id, "; ".join(set_))
        if rt.form_hints and b.roof.form == "unknown":
            form = rt.form_hints.most_common(1)[0][0]
            if form in {"hip", "gable", "skillion", "flat"}:
                b.roof.form = form  # type: ignore[assignment]

    claude_ok = use_claude and api_available()
    if use_claude and not claude_ok:
        attention.append("ANTHROPIC_API_KEY not set - load-bearing walls and geotech left for manual review")

    # 2. Load-bearing walls + roof confirmation, one call per storey.
    for b in project.buildings:
        for s in b.storeys:
            if not s.walls or s.frame is None:
                continue
            internal = [w for w in s.walls if not w.external]
            if not internal and b.roof.form != "unknown":
                continue
            if not claude_ok:
                attention.append(f"{b.id}/{s.id}: decide load-bearing for {len(internal)} internal walls")
                continue
            try:
                labels = label_storey(arch_pdf, project, b, s, totals, context_pages=ctx_pages[:2])
                attention.extend(f"{b.id}/{s.id}: {n}" for n in apply_storey_labels(b, s, labels))
            except Exception as e:  # keep going; the review screen can still fix it by hand
                log.exception("labelling %s/%s failed", b.id, s.id)
                attention.append(f"{b.id}/{s.id}: Claude labelling failed ({e}) - decide load-bearing manually")

    # 3. Geotech report.
    if geotech_pdf is None:
        attention.append("No geotechnical report - site class must be entered in review")
    elif claude_ok:
        try:
            attention.extend(parse_geotech(geotech_pdf, project.site, totals))
        except Exception as e:
            log.exception("geotech parse failed")
            attention.append(f"Geotech parse failed ({e}) - enter site data manually")
    else:
        attention.append("Geotech report supplied but not parsed (no API key)")

    return totals
