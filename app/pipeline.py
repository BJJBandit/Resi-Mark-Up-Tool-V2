"""End-to-end review: geotech -> architectural assessment -> mark-up sheets -> PDF."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import claude
from .models import Ledger, choose_sheet_models
from .pdf import build_markup_pdf, render_pages
from .rules import apply_strip_rule, check_geotech_strip
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
    references: list[tuple[str, bytes]] = field(default_factory=list)  # (filename, pdf)
    job_no: str = ""
    title_block: bool = False
    pages: list[int] = field(default_factory=list)  # selected drawing pages (1-based); empty = all
    rev: str = "A"
    made_by: str = ""
    sheet_model: str = "auto"  # auto | model id for the mark-up sheets


@dataclass
class JobResult:
    geotech: GeotechReview
    design: DesignAssessment
    sheets: dict[SheetKind, MarkupSheet] = field(default_factory=dict)
    sheet_errors: dict[SheetKind, str] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sheet_models: dict[SheetKind, tuple[str, str]] = field(default_factory=dict)  # model, reason
    pdf: bytes = b""


def run(
    job: JobInput,
    progress: Callable[[str], None] = lambda _: None,
    ledger: Ledger | None = None,
) -> JobResult:
    """ledger collects the cost of every Claude call (kept by the caller, so the
    cost is known even if the review fails part-way)."""
    ledger = ledger if ledger is not None else Ledger()
    progress("Rendering architectural drawings")
    pages = render_pages(job.arch_pdf, job.pages or None)

    refs: list[claude.Reference] = []
    if job.references:
        progress(f"Uploading {len(job.references)} reference document(s)")
        refs = claude.upload_references(job.references)
    try:
        return _run(job, pages, refs, progress, ledger)
    finally:
        claude.delete_references(refs)


def _run(job: JobInput, pages, refs, progress: Callable[[str], None], ledger: Ledger) -> JobResult:
    progress("Step 1: reviewing geotechnical report and wind")
    geotech = claude.review_geotech(job.geotech_pdf, job.wind_class, job.site_class, refs, ledger=ledger)

    progress("Step 2: reviewing architectural drawings")
    design = claude.assess_design(pages, geotech, job.construction, job.notes, refs, ledger=ledger)
    result = JobResult(geotech=geotech, design=design, references=[r.title for r in refs])
    # Firm rules are enforced on the output, not just requested in the prompt.
    result.warnings += check_geotech_strip([*geotech.recommendations, geotech.founding])
    result.warnings += apply_strip_rule(design, "Design assessment")

    # Documents reviewed: now pick the most cost-effective model for each sheet.
    result.sheet_models = choose_sheet_models(geotech, design, job.sheet_model)
    sheet_warnings: dict[SheetKind, list[str]] = {}

    def make(kind: SheetKind) -> None:
        try:
            model = result.sheet_models[kind][0]
            sheet = claude.plan_sheet(kind, pages, geotech, design, refs, model=model, ledger=ledger)
            sheet_warnings[kind] = apply_strip_rule(sheet, SHEET_TITLES[kind])
            result.sheets[kind] = sheet
        except Exception as e:  # keep the other sheets if one fails
            result.sheet_errors[kind] = str(e) or type(e).__name__

    # The prompt cache is per model: run one sheet per model first so it caches the
    # drawings, then the rest in parallel read them from the cache.
    firsts, rest, seen = [], [], set()
    for kind in SheetKind:
        m = result.sheet_models[kind][0]
        (rest if m in seen else firsts).append(kind)
        seen.add(m)
    progress(f"Step 3: marking up sheets (first {len(firsts)} of 5)")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(make, firsts))
    progress("Step 3: marking up remaining sheets")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(make, rest))

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
