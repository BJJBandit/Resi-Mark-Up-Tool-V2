"""End-to-end review: geotech -> architectural assessment -> mark-up sheets -> PDF."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import claude, refstore, usage
from .pdf import build_markup_pdf, extract_pages, format_ranges, render_pages
from .refpages import MAX_REF_PAGES, RefDoc, prepare, report
from .rules import apply_rules, check_geotech
from .schemas import SHEET_TITLES, DesignAssessment, GeotechReview, MarkupSheet, SheetKind


@dataclass
class JobInput:
    project: str
    arch_pdf: bytes
    geotech_pdf: bytes | None
    wind_class: str | None
    site_class: str | None
    construction: str
    notes: str
    references: list[RefDoc] = field(default_factory=list)  # pages=None -> chosen from the contents
    job_no: str = ""
    title_block: bool = False
    pages: list[int] = field(default_factory=list)  # selected drawing pages (1-based); empty = all
    rev: str = "A"
    made_by: str = ""
    sheet_notes: list[str] = field(default_factory=list)  # engineer's notes added to every sheet as written


@dataclass
class JobResult:
    geotech: GeotechReview
    design: DesignAssessment
    sheets: dict[SheetKind, MarkupSheet] = field(default_factory=dict)
    sheet_errors: dict[SheetKind, str] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    reference_pages: list[dict] = field(default_factory=list)  # pages of each standard / guide used
    warnings: list[str] = field(default_factory=list)
    pdf: bytes = b""
    usage: dict = field(default_factory=dict)  # tokens and estimated cost of every Claude call


def _joint(tag: str) -> bool:
    return re.match(r"\s*(AJ|CJ)\d*\b", tag or "", re.I) is not None


def _norm(note: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", note.upper()).strip()


def _as_note(note: str) -> str:
    note = note.strip().upper()  # house style: notes in capitals
    return note if note.startswith("-") else f"-{note}"


def _masonry(design: DesignAssessment) -> bool:
    text = f"{design.external_wall_type} {design.framing_material} {design.governing_standard}"
    return re.search(r"brick|masonry|block|veneer|AS ?3700|AS ?4773", text, re.I) is not None


def _stored_references(docs: list[RefDoc], progress: Callable[[str], None]):
    """Read each document's chosen pages from the reference store, transcribing (once,
    ever) any page not stored yet. Pages that can't be transcribed go as PDF pages.
    Returns (text references, [(name, pdf)] still to upload, warnings)."""
    refs: list[claude.Reference] = []
    files: list[tuple[str, bytes]] = []
    warnings: list[str] = []
    for d in docs:
        if not d.pages:
            continue
        key = refstore.doc_key(d.data)

        def transcribe(chunk: list[int], d=d) -> dict[int, str]:
            return claude.transcribe_reference_pages(d.stem, extract_pages(d.data, chunk), chunk)

        texts, new, failed = refstore.get_text(key, d.name, d.page_count, d.pages, transcribe, progress)
        reused = len(d.pages) - new - len(failed)
        parts = []
        if reused:
            parts.append(f"{reused} from stored extracts")
        if new:
            parts.append(f"{new} newly extracted and stored")
        if failed:
            parts.append(f"{len(failed)} sent as PDF pages")
        d.store = "; ".join(parts)
        if texts:
            refs.append(claude.Reference(title=f"{d.stem} (text extract of PDF pages {format_ranges(sorted(texts))})",
                                         text=refstore.as_text(d.stem, texts)))
        if failed:
            warnings.append(f"{d.stem}: pages {format_ranges(failed)} could not be extracted to text, so they "
                            "were sent as PDF pages (they will be tried again next time).")
            files.append((f"{d.stem} (pages {format_ranges(failed)} of {d.page_count}).pdf",
                          extract_pages(d.data, failed)))
    return refs, files, warnings


def run(job: JobInput, progress: Callable[[str], None] = lambda _: None) -> JobResult:
    log = usage.start()
    try:
        result = _run_all(job, progress)
    finally:
        s = log.summary()
        print(f"[usage] review total: {len(s['calls'])} calls, est. ${s['cost_usd']:.2f}, "
              f"cache hit {s['cache_hit_rate']:.0%}", flush=True)
    result.usage = s
    return result


def _run_all(job: JobInput, progress: Callable[[str], None]) -> JobResult:
    progress("Rendering architectural drawings")
    pages = render_pages(job.arch_pdf, job.pages or None)

    refs: list[claude.Reference] = []
    ref_warnings: list[str] = []
    if job.references:
        context = (
            f"Construction: {job.construction}\nWind class: {job.wind_class or 'not given'}\n"
            f"Site class: {job.site_class or 'from geotech report'}\nEngineer's direction: {job.notes or 'none'}\n"
            "Drawing text (excerpt): " + " ".join(p.text for p in pages)[:3000]
        )
        files, ref_warnings = prepare(job.references, context, claude.select_reference_pages, MAX_REF_PAGES, progress)
        if refstore.enabled():
            refs, files, more = _stored_references(job.references, progress)
            ref_warnings += more
        if files:
            progress(f"Uploading {len(files)} reference document(s)")
            refs += claude.upload_references(files)
    try:
        result = _run(job, pages, refs, progress)
        result.reference_pages = report(job.references)
        result.warnings = ref_warnings + result.warnings
        return result
    finally:
        claude.delete_references(refs)


def _run(job: JobInput, pages, refs, progress: Callable[[str], None]) -> JobResult:
    progress("Step 1: reviewing geotechnical report and wind")
    geotech = claude.review_geotech(job.geotech_pdf, job.wind_class, job.site_class, refs, job.notes)

    progress("Step 2: reviewing architectural drawings")
    design = claude.assess_design(pages, geotech, job.construction, job.notes, refs)
    result = JobResult(geotech=geotech, design=design, references=[r.title for r in refs])
    # Claude's cross-reference of the plan and elevation views.
    result.warnings += [f"Plan vs elevations: {x}" for x in design.opening_crosscheck]
    result.warnings += [
        f"Plan vs elevation dimensions - {d.item}: plan {d.plan}, elevation {d.elevation}"
        + (f" ({d.note})" if d.note else "")
        for d in design.dimension_crosscheck if d.status.strip().lower().startswith("mismatch")
    ]
    # Firm (office) rules are enforced on the output, not just requested in the prompt.
    result.warnings += check_geotech([*geotech.recommendations, geotech.founding])
    result.warnings += apply_rules(design, "Design assessment")

    sheet_warnings: dict[SheetKind, list[str]] = {}

    def make(kind: SheetKind) -> None:
        try:
            sheet = claude.plan_sheet(kind, pages, geotech, design, refs, job.notes)
            if kind != SheetKind.ARTICULATION:
                # Articulation / control joints belong on the elevation sheet only.
                dropped = [e for e in sheet.elements if e.kind == "member" and _joint(e.tag)]
                if dropped:
                    sheet.elements = [e for e in sheet.elements if e not in dropped]
            sheet_warnings[kind] = apply_rules(sheet, SHEET_TITLES[kind])
            # The engineer's own notes go on every sheet word for word (after the rules,
            # so they are never rewritten), skipping any Claude already wrote.
            have = {_norm(n) for n in sheet.notes}
            sheet.notes += [_as_note(n) for n in job.sheet_notes if _norm(n) not in have]
            result.sheets[kind] = sheet
        except Exception as e:  # keep the other sheets if one fails
            result.sheet_errors[kind] = str(e) or type(e).__name__

    kinds = [k for k in SheetKind if k != SheetKind.ARTICULATION or _masonry(design)]
    progress(f"Step 3: marking up sheets (1 of {len(kinds)})")
    make(kinds[0])  # first call warms the prompt cache for the rest
    progress("Step 3: marking up remaining sheets")
    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in [usage.submit(pool, make, k) for k in kinds[1:]]:
            f.result()

    for kind in SheetKind:
        result.warnings += sheet_warnings.get(kind, [])

    progress("Building mark-up PDF")
    result.pdf, build_warnings = build_markup_pdf(
        job.arch_pdf,
        job.project,
        geotech,
        design,
        result.sheets,
        result.references,
        job_no=job.job_no,
        rev=job.rev,
        made_by=job.made_by,
        title_block=job.title_block,
    )
    result.warnings += build_warnings
    return result
