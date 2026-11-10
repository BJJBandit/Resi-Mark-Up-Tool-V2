"""End-to-end review: geotech -> architectural assessment -> mark-up sheets -> PDF."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import claude, refstore, spelling, stock, usage
from .pdf import build_markup_pdf, extract_pages, format_ranges, render_pages
from .refpages import MAX_REF_PAGES, RefDoc, prepare, report
from .rules import apply_rules, check_geotech
from .schemas import SHEET_TITLES, DesignAssessment, GeotechReview, MarkupSheet, SheetKind


@dataclass
class JobInput:
    project: str
    arch_pdf: bytes
    geotech_pdf: bytes | None | list[tuple[str, bytes]]  # one report, or [(name, pdf)] for several
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
    sheet_notes: list[str] = field(default_factory=list)
    sheets: list[SheetKind] = field(default_factory=list)  # sheets to produce; empty = all
    combine_foundation: bool = True  # footings + floor framing / slab on one sheet
    page_labels: list[str] = field(default_factory=list)  # source file + page of each page of arch_pdf  # engineer's notes added to every sheet as written


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
    # Kept so one sheet can be re-run without repeating the review (see rerun_sheet).
    base_warnings: list[str] = field(default_factory=list)  # references + design assessment
    sheet_warnings: dict[SheetKind, list[str]] = field(default_factory=dict)
    build_warnings: list[str] = field(default_factory=list)
    # Sheets as planned by Claude (after the rules), before any snapping / checks. Every PDF
    # build starts from copies of these: building moves members and adds check clouds, and
    # doing that twice (a re-run rebuilds every sheet) would duplicate the clouds.
    planned: dict[SheetKind, MarkupSheet] = field(default_factory=dict)
    reruns: list[dict] = field(default_factory=list)  # {"sheet", "note", "ok", "detail"}
    ctx: "RerunContext | None" = field(default=None, repr=False)

    def compose_warnings(self) -> None:
        self.warnings = self.base_warnings + [w for k in SheetKind for w in self.sheet_warnings.get(k, [])] \
            + self.build_warnings


@dataclass
class RerunContext:
    job: JobInput
    pages: list  # rendered drawing pages, exactly as sent the first time (so the cache still matches)
    text_refs: list  # stored text extracts of the standards (claude.Reference)
    files: list[tuple[str, bytes]]  # standards pages sent as PDF (re-uploaded for a re-run)
    log: usage.UsageLog


def _joint(tag: str) -> bool:
    return re.match(r"\s*(AJ|CJ)\d*\b", tag or "", re.I) is not None


def _norm(note: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", note.upper()).strip()


def _as_note(note: str) -> str:
    note = note.strip().upper()  # house style: notes in capitals
    return note if note.startswith("-") else f"-{note}"


_SPACING = re.compile(r"\b(?:AT\s+)?(?:MAX\.?\s+)?\d{3,4}\s*(?:MM\s*)?(?:CTRS|CENTRES|CENTERS|CRS)\b", re.I)


_REO = re.compile(r"\b\d+\s*/\s*[LN]\d{2}\s*TM?\b(?:\s+(?:TOP\s*(?:AND|&)\s*BOTTOM|TOP|BOTTOM|T&B))?|\bSL\d{2,3}\b(?:\s+TOP)?|\b[LN]\d{2}\s+(?:LIGS|LIGATURES)\b[^,.;]*", re.I)


def short_spec(text: str) -> str:
    """Schedule / legend text cut to the member type (size, grade, member, reinforcement)
    and its spacing: everything after the first note break ('. ', ' - ', ' (', '; ') goes,
    and a spacing given later is kept."""
    t = " ".join((text or "").split())
    cut = len(t)
    for sep in (". ", " - ", " – ", " (", "; ", " -- "):
        i = t.find(sep)
        # Too early to be the end of the member type ('TYPE (h) ...'), or a short code in
        # brackets that IS part of the type ('PANEL TYPE (h)', 'TYPE (g)'): keep looking.
        while i != -1 and (i < 15 or (sep == " (" and re.match(r" \([A-Za-z0-9]{1,4}\)", t[i:]))):
            i = t.find(sep, i + 1)
        if 0 < i < cut:
            cut = i
    head = t[:cut].rstrip(" .,;:-")
    rest = t[cut:]
    reo = [m.group(0).upper() for m in _REO.finditer(rest) if m.group(0).upper() not in head.upper()]
    if reo:  # footings / slabs: keep the reinforcement, it is part of the type
        head += ", " + ", ".join(dict.fromkeys(reo))
    if not _SPACING.search(head):
        m = _SPACING.search(rest)
        if m:
            sp = m.group(0).upper()
            head += " " + (sp if sp.startswith("AT") else "AT " + sp)
    return head or t


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
        result = _run_all(job, progress, log)
    finally:
        s = _report_usage(log, "review")
    result.usage = s
    return result


def _report_usage(log: usage.UsageLog, what: str) -> dict:
    s = log.summary()
    print(f"[usage] {what} total: {len(s['calls'])} calls, est. ${s['cost_usd']:.2f}, "
          f"cache hit {s['cache_hit_rate']:.0%}", flush=True)
    for chk in s["cache_checks"]:
        mark = {True: "YES", False: "NO", None: "n/a"}[chk["ok"]]
        print(f"[cache] {chk['check']}: {mark} - {chk['detail']}", flush=True)
    return s


def _run_all(job: JobInput, progress: Callable[[str], None], log: usage.UsageLog) -> JobResult:
    progress("Rendering architectural drawings")
    pages = render_pages(job.arch_pdf, job.pages or None)
    if job.page_labels and len({lb.rsplit(" page ", 1)[0] for lb in job.page_labels}) > 1:
        for p in pages:  # several drawing sets: say which file each page came from
            if 0 < p.number <= len(job.page_labels):
                p.label = job.page_labels[p.number - 1]

    refs: list[claude.Reference] = []
    ref_warnings: list[str] = []
    files: list[tuple[str, bytes]] = []
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
    text_refs = list(refs)
    if files:
        progress(f"Uploading {len(files)} reference document(s)")
        refs += claude.upload_references(files)
    try:
        result = _run(job, pages, refs, progress)
        result.reference_pages = report(job.references)
        result.base_warnings = ref_warnings + result.base_warnings
        result.compose_warnings()
        result.ctx = RerunContext(job=job, pages=pages, text_refs=text_refs, files=files, log=log)
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
    result.base_warnings += [f"Plan vs elevations: {x}" for x in design.opening_crosscheck]
    result.base_warnings += [
        f"Plan vs elevation dimensions - {d.item}: plan {d.plan}, elevation {d.elevation}"
        + (f" ({d.note})" if d.note else "")
        for d in design.dimension_crosscheck if d.status.strip().lower().startswith("mismatch")
    ]
    # Firm (office) rules are enforced on the output, not just requested in the prompt.
    result.base_warnings += check_geotech([*geotech.recommendations, geotech.founding])
    result.base_warnings += apply_rules(design, "Design assessment")

    def make(kind: SheetKind) -> None:
        for attempt in (1, 2):  # one retry: a dropped stream or bad answer shouldn't lose the sheet
            try:
                _make_sheet(kind, job, pages, refs, result)
                return
            except Exception as e:  # keep the other sheets if one fails
                print(f"[sheet] {SHEET_TITLES[kind]} attempt {attempt} failed: {e!r}", flush=True)
                result.sheet_errors[kind] = str(e) or type(e).__name__

    kinds = [k for k in _wanted(job) if k != SheetKind.ARTICULATION or _masonry(design)]
    if not kinds:  # only the articulation sheet was ticked, and this isn't a masonry job
        result.base_warnings.append("Only the articulation joint sheet was selected, but the walls aren't "
                                    "masonry, so no sheets were produced.")
        progress("Building mark-up PDF")
        _build(job, result)
        return result
    progress(f"Step 3: marking up sheets (1 of {len(kinds)})")
    make(kinds[0])  # first call warms the prompt cache for the rest
    progress("Step 3: marking up remaining sheets")
    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in [usage.submit(pool, make, k) for k in kinds[1:]]:
            f.result()

    progress("Building mark-up PDF")
    _build(job, result)
    return result


def _make_sheet(kind: SheetKind, job: JobInput, pages, refs, result: JobResult, change: str = "") -> None:
    """Plan one sheet and store it (with its rule warnings) on the result. Raises on failure."""
    direction = job.notes
    if change:
        direction = (f"{direction}\n" if direction else "") + \
            f"Re-run of this sheet - the engineer asked for this change: {change}"
    sheet = claude.plan_sheet(kind, pages, result.geotech, result.design, refs, direction)
    if kind != SheetKind.ARTICULATION:
        # Articulation / control joints belong on the elevation sheet only.
        dropped = [e for e in sheet.elements if e.kind == "member" and _joint(e.tag)]
        if dropped:
            sheet.elements = [e for e in sheet.elements if e not in dropped]
    result.sheet_warnings[kind] = apply_rules(sheet, SHEET_TITLES[kind])
    # House style: schedules list only the member type and spacing - no notes.
    sheet.schedule = [s.model_copy(update={"text": short_spec(s.text)}) for s in sheet.schedule]
    result.sheet_warnings[kind] += stock.check(sheet.schedule, SHEET_TITLES[kind])
    sheet.legend = [s.model_copy(update={"text": short_spec(s.text)}) for s in sheet.legend]
    # Australian English on everything written on the sheet (catches American spellings).
    sheet.notes = [spelling.to_au(n) for n in sheet.notes]
    sheet.classification = spelling.to_au(sheet.classification)
    sheet.elements = [e.model_copy(update={"text": spelling.to_au(e.text)}) for e in sheet.elements]
    sheet.schedule = [s.model_copy(update={"text": spelling.to_au(s.text)}) for s in sheet.schedule]
    sheet.legend = [s.model_copy(update={"text": spelling.to_au(s.text)}) for s in sheet.legend]
    # The engineer's own notes go on every sheet word for word (after the rules,
    # so they are never rewritten), skipping any Claude already wrote.
    have = {_norm(n) for n in sheet.notes}
    sheet.notes += [_as_note(n) for n in job.sheet_notes if _norm(n) not in have]
    result.planned[kind] = sheet
    result.sheets[kind] = sheet
    result.sheet_errors.pop(kind, None)


def _build(job: JobInput, result: JobResult) -> None:
    result.sheets = {k: result.planned[k].model_copy(deep=True) for k in SheetKind if k in result.planned}
    result.pdf, result.build_warnings = build_markup_pdf(
        job.arch_pdf,
        job.project,
        result.geotech,
        result.design,
        result.sheets,
        result.references,
        job_no=job.job_no,
        rev=job.rev,
        made_by=job.made_by,
        title_block=job.title_block,
    )
    result.compose_warnings()


def _wanted(job: JobInput) -> list[SheetKind]:
    """Sheets to produce, in drawing order: footings and floor become one FOOTING & FLOOR
    sheet when combining is ticked and both are wanted."""
    split = (SheetKind.FOOTINGS, SheetKind.FLOOR)
    if job.sheets:
        wanted = list(job.sheets)
        if SheetKind.FOUNDATION in wanted:  # the combined sheet covers both
            wanted = [k for k in wanted if k not in split]
    else:  # no choice given (API clients): every sheet
        wanted = [k for k in SheetKind if k != SheetKind.FOUNDATION]
    if job.combine_foundation and all(k in wanted for k in split):
        wanted = [k for k in wanted if k not in split] + [SheetKind.FOUNDATION]
    return [k for k in SheetKind if k in wanted]


def can_rerun(result: JobResult, kind: SheetKind) -> bool:
    """Sheets offered for re-run / adding: those made, plus the others for this review's
    combine-footings-and-floor choice."""
    if result.ctx is None or (kind == SheetKind.ARTICULATION and not _masonry(result.design)):
        return False
    if kind in result.sheets:
        return True
    split = (SheetKind.FOOTINGS, SheetKind.FLOOR)
    if SheetKind.FOUNDATION in result.sheets:
        combined = True  # footings and floor already on one sheet: don't offer them again
    elif any(k in result.sheets for k in split):
        combined = False  # one of them made on its own: offer the other on its own
    else:
        job = result.ctx.job
        combined = SheetKind.FOUNDATION in job.sheets or (
            not any(k in job.sheets for k in split) and job.combine_foundation)
    return kind not in split if combined else kind != SheetKind.FOUNDATION


def rerun_sheet(result: JobResult, kind: SheetKind, change: str = "",
                progress: Callable[[str], None] = lambda _: None) -> None:
    """Re-plan one sheet from the stored review (geotech + design assessment are not
    repeated) and rebuild the PDF. If the re-run fails the previous sheet is kept."""
    ctx = result.ctx
    assert ctx is not None
    usage.use(ctx.log)  # the re-run's calls count toward this review's usage
    title = SHEET_TITLES[kind]
    refs = list(ctx.text_refs)
    uploaded: list = []
    entry = {"sheet": title, "note": change, "ok": True, "detail": ""}
    try:
        if ctx.files:
            progress(f"Re-running {title}: uploading reference pages")
            uploaded = claude.upload_references(ctx.files)
        progress(f"Re-running {title}")
        n0 = len(ctx.log.calls)
        try:
            _make_sheet(kind, ctx.job, ctx.pages, refs + uploaded, result, change)
            cost = sum(c.cost_usd or 0 for c in ctx.log.calls[n0:])
            entry["detail"] = f"re-run complete (est. US${cost:.2f})"
        except Exception as e:
            entry.update(ok=False, detail=(f"re-run failed ({e or type(e).__name__}); "
                                           + ("the previous version is kept" if kind in result.sheets
                                              else "sheet still not produced")))
            if kind not in result.sheets:
                result.sheet_errors[kind] = str(e) or type(e).__name__
    finally:
        claude.delete_references(uploaded)
    progress("Rebuilding mark-up PDF")
    _build(ctx.job, result)
    result.reruns.append(entry)
    result.usage = _report_usage(ctx.log, f"review + re-run of {title}")
