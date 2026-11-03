"""End-to-end review: geotech -> architectural assessment -> mark-up sheets -> PDF."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import claude
from .pdf import build_markup_pdf, render_pages
from .schemas import DesignAssessment, GeotechReview, MarkupSheet, SheetKind


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
    rev: str = "A"
    made_by: str = ""


@dataclass
class JobResult:
    geotech: GeotechReview
    design: DesignAssessment
    sheets: dict[SheetKind, MarkupSheet] = field(default_factory=dict)
    sheet_errors: dict[SheetKind, str] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pdf: bytes = b""


def run(job: JobInput, progress: Callable[[str], None] = lambda _: None) -> JobResult:
    progress("Rendering architectural drawings")
    pages = render_pages(job.arch_pdf)

    refs: list[claude.Reference] = []
    if job.references:
        progress(f"Uploading {len(job.references)} reference document(s)")
        refs = claude.upload_references(job.references)
    try:
        return _run(job, pages, refs, progress)
    finally:
        claude.delete_references(refs)


def _run(job: JobInput, pages, refs, progress: Callable[[str], None]) -> JobResult:
    progress("Step 1: reviewing geotechnical report and wind")
    geotech = claude.review_geotech(job.geotech_pdf, job.wind_class, job.site_class, refs)

    progress("Step 2: reviewing architectural drawings")
    design = claude.assess_design(pages, geotech, job.construction, job.notes, refs)
    result = JobResult(geotech=geotech, design=design, references=[r.title for r in refs])

    def make(kind: SheetKind) -> None:
        try:
            result.sheets[kind] = claude.plan_sheet(kind, pages, geotech, design, refs)
        except Exception as e:  # keep the other sheets if one fails
            result.sheet_errors[kind] = str(e)

    kinds = list(SheetKind)
    progress("Step 3: marking up sheets (1 of 5)")
    make(kinds[0])  # first call warms the prompt cache for the rest
    progress("Step 3: marking up remaining sheets")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(make, kinds[1:]))

    progress("Building mark-up PDF")
    result.pdf, result.warnings = build_markup_pdf(
        job.arch_pdf,
        job.project,
        geotech,
        design,
        result.sheets,
        result.references,
        job_no=job.job_no,
        rev=job.rev,
        made_by=job.made_by,
    )
    return result
