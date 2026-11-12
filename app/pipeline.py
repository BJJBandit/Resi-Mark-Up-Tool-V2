"""End-to-end review: geotech -> architectural assessment -> mark-up sheets -> PDF."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from . import claude, refstore, spelling, stock, usage
from .pdf import build_markup_pdf, extract_pages, format_ranges, render_pages
from .refpages import MAX_REF_PAGES, RefDoc, prepare, report
from .rules import apply_rules, check_geotech
from .schemas import (SHEET_TITLES, DesignAssessment, Element, GeotechReview, InfoRequest, MarkupSheet, Point,
                      ScheduleItem, SheetKind)


class ReviewStopped(BaseException):
    """The engineer stopped the review. A BaseException, so the 'never lose the review over a
    side step' handlers (except Exception) don't swallow it; the runner catches it."""


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
    calc_sheet: bool = True  # add the calculation sheet (racking / bracing check, member working)
    page_labels: list[str] = field(default_factory=list)  # source file + page of each page of arch_pdf
    # Asks the engineer about the information step 2 says is missing and waits for the
    # answers: ask([request dicts]) -> [{"answer": "yes"|"no"|"ignore", "files": [(name,
    # pdf)], "text": str}]. None (no one to ask): every request is treated as "no".
    ask: Callable[[list[dict]], list[dict]] | None = field(default=None, repr=False)
    # Pause point: checkpoint(stage) -> None to carry on, or the engineer's changes made while
    # paused: {"notes", "sheet_notes", "construction", "sheets"} (any subset).
    checkpoint: Callable[[str], dict | None] | None = field(default=None, repr=False)


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


def _abbrev(before: str) -> bool:
    """The full stop ending 'before' belongs to an abbreviation ('G.I', 'CL', 'NO'), not a sentence."""
    word = before.split()[-1] if before.split() else ""
    return "." in word or (word.isalpha() and len(word) <= 2) or word.upper() in ("CL", "NO", "APPROX", "MIN", "MAX")


def short_spec(text: str) -> str:
    """Schedule / legend text cut to the member type (size, grade, member, reinforcement)
    and its spacing: everything after the first note break ('. ', ' - ', ' (', '; ') goes,
    and a spacing given later is kept."""
    t = " ".join((text or "").split())
    cut = len(t)
    for sep in (". ", " - ", " – ", " (", "; ", " -- "):
        i = t.find(sep)
        # Too early to be the end of the member type ('TYPE (h) ...'), or a short code in
        # brackets that IS part of the type (one or two letters) ('PANEL TYPE (h)', 'TYPE (g)'): keep looking.
        while i != -1 and (i < 15 or (sep == " (" and re.match(r" \([A-Za-z]{1,2}\)", t[i:]))
                           or (sep == ". " and _abbrev(t[:i]))):
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
    extra: dict = {"text_refs": [], "files": []}  # from the engineer's answers to step 2's requests
    try:
        result = _run(job, pages, refs, progress, extra)
        result.reference_pages = report(job.references)
        result.base_warnings = ref_warnings + result.base_warnings
        result.compose_warnings()
        result.ctx = RerunContext(job=job, pages=pages, text_refs=text_refs + extra["text_refs"],
                                  files=files + extra["files"], log=log)
        return result
    finally:
        claude.delete_references(refs)


def _apply_changes(job: JobInput, changes: dict | None) -> set[str]:
    """Apply the changes made while paused; returns which settings changed."""
    if not changes:
        return set()
    changed = set()
    if "notes" in changes and changes["notes"].strip() != job.notes.strip():
        job.notes = changes["notes"].strip()
        changed.add("notes")
    if "sheet_notes" in changes:
        new = [n.strip() for n in changes["sheet_notes"] if n.strip()]
        if new != job.sheet_notes:
            job.sheet_notes = new
            changed.add("sheet_notes")
    if changes.get("construction") and changes["construction"].strip() != job.construction:
        job.construction = changes["construction"].strip()
        changed.add("construction")
    if "sheets" in changes:
        new_sheets = [SheetKind(k) for k in changes["sheets"]]
        if new_sheets != job.sheets:
            job.sheets, job.combine_foundation = new_sheets, False
            changed.add("sheets")
    return changed


def _pause(job: JobInput, stage: str, progress: Callable[[str], None]) -> set[str]:
    if job.checkpoint is None:
        return set()
    try:
        changes = job.checkpoint(stage)
    except Exception as e:  # never lose the review over a pause
        print(f"[pause] {e!r}", flush=True)
        return set()
    changed = _apply_changes(job, changes)
    if changed:
        progress(f"Continuing with the changes ({', '.join(sorted(changed))})")
    return changed


def _run(job: JobInput, pages, refs, progress: Callable[[str], None], extra: dict | None = None) -> JobResult:
    progress("Step 1: reviewing geotechnical report and wind")
    geotech = claude.review_geotech(job.geotech_pdf, job.wind_class, job.site_class, refs, job.notes)
    _pause(job, "after step 1 (geotechnical review)", progress)

    extra = extra if extra is not None else {"text_refs": [], "files": []}

    def assess() -> tuple:
        progress("Step 2: reviewing architectural drawings")
        d = claude.assess_design(pages, geotech, job.construction, job.notes, refs)
        return _information(job, pages, refs, geotech, d, progress, extra)

    design, info_warnings = assess()
    # Changing the material / standard or the direction after the drawing review means redoing it.
    while _pause(job, "after step 2 (drawing review)", progress) & {"construction", "notes"}:
        design, info_warnings = assess()
    result = JobResult(geotech=geotech, design=design, references=[r.title for r in refs])
    result.base_warnings += info_warnings
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

    def todo() -> list[SheetKind]:
        return [k for k in _wanted(job) if (k != SheetKind.ARTICULATION or _masonry(design))
                and k not in result.planned]

    def drop_unwanted() -> None:  # sheets unticked while paused
        wanted = set(_wanted(job))
        for k in list(result.planned):
            if k not in wanted:
                result.planned.pop(k, None)
                result.sheet_warnings.pop(k, None)
        for k in list(result.sheet_errors):
            if k not in wanted:
                result.sheet_errors.pop(k)

    first = True
    while True:
        kinds = todo()
        if kinds:
            if first:
                progress(f"Step 3: marking up sheets (1 of {len(kinds)})")
                make(kinds[0])  # first call warms the prompt cache for the rest
                kinds = kinds[1:]
                first = False
                if "sheets" in _pause(job, "after the first sheet", progress):
                    drop_unwanted()
                kinds = todo()
            if kinds:
                progress("Step 3: marking up remaining sheets" if result.planned else "Step 3: marking up sheets")
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for f in [usage.submit(pool, make, k) for k in kinds]:
                        f.result()
        changed = _pause(job, "before the PDF is built", progress)
        if "sheets" in changed:
            drop_unwanted()
        if not ("sheets" in changed and todo()):
            break
    if not result.planned and not result.sheet_errors:
        result.base_warnings.append("No sheets were produced (only the articulation joint sheet was selected and "
                                    "the walls aren't masonry, or no sheets were selected).")
    progress("Building mark-up PDF")
    _build(job, result)
    return result


def _information(job: JobInput, pages, refs: list, geotech: GeotechReview, design: DesignAssessment,
                 progress: Callable[[str], None], extra: dict) -> tuple[DesignAssessment, list[str]]:
    """Ask the engineer about the standards / documents / information step 2 says are missing.
    Yes: the uploads (standards are kept in the reference store, so they're read once) and any
    typed information are added and step 2 runs again with them. No: proceed with the stated
    assumption (recorded in the direction and assumptions). Ignore: proceed, nothing recorded
    on the sheets."""
    reqs: list[InfoRequest] = list(design.information_requests)
    if not reqs:
        return design, []
    answers: list[dict] = []
    if job.ask is not None:
        progress("Waiting for your answers: more information requested")
        try:
            answers = job.ask([r.model_dump() for r in reqs]) or []
        except Exception as e:  # never lose the review over the question
            print(f"[info] asking failed: {e!r}", flush=True)
    warnings, direction, assumed = [], [], []
    typed = False  # information typed in as an answer
    std_docs: list[RefDoc] = []
    other_docs: list[RefDoc] = []
    for i, r in enumerate(reqs):
        a = answers[i] if i < len(answers) and answers[i] else {}
        ans = str(a.get("answer", "no")).lower()
        files = a.get("files") or []
        text = str(a.get("text") or "").strip()
        if ans == "yes" and (files or text):
            for name, data in files:
                try:
                    with pymupdf.open(stream=data, filetype="pdf") as d:
                        count = d.page_count
                except Exception:
                    warnings.append(f"{r.item}: '{name}' could not be opened as a PDF - not used")
                    continue
                (std_docs if r.kind == "standard" else other_docs).append(RefDoc(name, data, count))
            if text:
                direction.append(f"{r.item}: {text}")
                typed = True
            warnings.append(f"More information supplied: {r.item}"
                            + (f" ({', '.join(n for n, _ in files)})" if files else ""))
        elif ans == "ignore":
            warnings.append(f"Information request ignored: {r.item}")
        else:  # no (or yes with nothing supplied, or no answer)
            direction.append(f"{r.item} is NOT available - proceed with the assumption: {r.assumption}")
            assumed.append(f"{r.item} not available - assumed: {r.assumption}")
            warnings.append(f"Not available, assumed: {r.item} - {r.assumption}")
    if direction:
        job.notes = (f"{job.notes}\n" if job.notes else "") + \
            "Engineer's answers to the information requests:\n" + "\n".join(f"- {d}" for d in direction)
    new_refs: list = []
    if std_docs or other_docs:
        progress("Reading the additional documents")
        context = f"Needed for: {'; '.join(f'{r.item} ({r.why})' for r in reqs)}"
        files: list[tuple[str, bytes]] = []
        texts: list = []
        if other_docs:
            got, more = prepare(other_docs, context, claude.select_reference_pages, MAX_REF_PAGES // 2, progress)
            files += got
            warnings += more
        if std_docs:
            got, more = prepare(std_docs, context, claude.select_reference_pages, MAX_REF_PAGES // 2, progress)
            warnings += more
            if refstore.enabled():
                for d in std_docs:  # kept, so later reviews can tick it instead of uploading it again
                    try:
                        refstore.save_pdf(refstore.doc_key(d.data), d.name, d.data, d.page_count)
                    except OSError as e:
                        print(f"[refstore] could not store {d.name}: {e}", flush=True)
                # Its pages are transcribed once and kept as text (read from the store next time).
                texts, got, more = _stored_references(std_docs, progress)
                warnings += more
            files += got
        uploaded = claude.upload_references(files) if files else []
        new_refs = texts + uploaded
        refs.extend(new_refs)
        extra["text_refs"] += texts
        extra["files"] += files
        job.references += std_docs + other_docs
    if new_refs or typed:
        progress("Step 2 again: reviewing the drawings with the additional information")
        design = claude.assess_design(pages, geotech, job.construction, job.notes, refs)
    design.assumptions = list(design.assumptions) + [a for a in assumed if a not in design.assumptions]
    design.information_requests = reqs  # what was asked (not asked again)
    return design, warnings


_DRIVEWAY = re.compile(r"\bDRIVE\s?WAYS?\b", re.I)
_BY_OTHERS = re.compile(r"BY OTHERS|NOT PART OF|EXCLUDED|BY OWNER", re.I)


_SHOWER = re.compile(r"SHOWER|WET AREA|BATHROOM|ENSUITE", re.I)
_ON_SLAB = re.compile(r"BONDEK|SUSPENDED (?:CONCRETE )?SLAB|EXISTING (?:CONCRETE |FIRST FLOOR )?SLAB|"
                      r"FIRST FLOOR SLAB|UPPER FLOOR SLAB", re.I)
_FALLS_COVERED = re.compile(r"SET[- ]?DOWN|SCREED", re.I)
WET_ON_SLAB_NOTE = ("-WET AREAS / HOB-FREE SHOWER ON THE EXISTING SUSPENDED SLAB: PROVIDE A SET-DOWN OR TOPPING "
                    "SCREED FOR THE FALLS TO THE WATERSTOP. CONFIRM THE EXISTING SLAB THICKNESS, ITS CAPACITY FOR "
                    "THE ADDED SCREED AND TILE LOAD, AND ANY CUT FOR A SET-DOWN - ENGINEER TO CONFIRM.")


def wet_area_on_slab(design: DesignAssessment, sheet: MarkupSheet, title: str) -> list[str]:
    """Office practice: a shower / wet area on an existing suspended slab needs a set-down or
    screed for its falls - add the engineer check note if the sheet doesn't cover it."""
    text = design.model_dump_json()
    if not (_SHOWER.search(text) and _ON_SLAB.search(text)):
        return []
    if any(_FALLS_COVERED.search(n) for n in sheet.notes) or \
            any(_FALLS_COVERED.search(e.text or "") for e in sheet.elements if e.kind == "callout"):
        return []
    sheet.notes.append(WET_ON_SLAB_NOTE)
    return [f"{title}: wet area on an existing suspended slab - set-down / screed note added (engineer to confirm)"]


EP_TEXT = "350x230 BRICK ENGAGED PIER BONDED INTO BASE WALL, ON P1"


def engaged_piers(sheet: MarkupSheet, title: str, aspect: float = 1.414) -> list[str]:
    """Office practice: a bearer ending on a brick base wall (strip footing SF) bears on an
    engaged pier bonded into the wall, over a bored pier. Each such bearer end gets an EP1
    (an existing pier there is re-tagged), and EP1 goes in the schedule. Returns warnings."""
    walls = [e for e in sheet.elements if e.kind == "member" and re.match(r"SF\d*$", e.tag.strip().upper())]
    bearers = [e for e in sheet.elements if e.kind == "member" and re.match(r"B\d+$", e.tag.strip().upper())]
    if not walls or not bearers:
        return []

    def xy(p):
        return (p.x * aspect, p.y)

    def foot(pt, a, b):
        (px, py), (ax, ay), (bx, by) = xy(pt), xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        fx, fy = ax + dx * t, ay + dy * t
        return ((px - fx) ** 2 + (py - fy) ** 2) ** 0.5, Point(x=fx / aspect, y=fy)

    def dist(p, q):
        return (((p.x - q.x) * aspect) ** 2 + (p.y - q.y) ** 2) ** 0.5

    tol = 0.014
    added = retagged = 0
    new_els = []
    for b in bearers:
        for end in (b.start, b.end):
            best = min((foot(end, w.start, w.end) for w in walls), key=lambda r: r[0])
            if best[0] > tol:
                continue
            at = best[1]
            near = [e for e in sheet.elements + new_els if e.kind == "pier" and dist(e.start, at) <= tol]
            if near:
                for e in near:
                    if e.tag.strip().upper() != "EP1":
                        e.tag = "EP1"
                        retagged += 1
                continue
            new_els.append(Element(kind="pier", tag="EP1", start=at, end=at, text=""))
            added += 1
    if not (added or retagged):
        return []
    sheet.elements += new_els
    if not any(s.tag.strip().upper() == "EP1" for s in sheet.schedule):
        sheet.schedule.append(ScheduleItem(tag="EP1", text=EP_TEXT))
    still = {e.tag.strip().upper() for e in sheet.elements}
    sheet.schedule = [s for s in sheet.schedule if s.tag.strip().upper() in still or not re.match(r"P\d+$", s.tag.strip().upper())]
    return [f"{title}: engaged piers EP1 at {added + retagged} bearer end(s) on the brick base walls "
            f"({added} added, {retagged} pier(s) re-tagged)"]


def drop_driveways(sheet: MarkupSheet, title: str) -> list[str]:
    """Office practice: driveways are ignored. Driveway slab members / schedule items,
    callouts and notes about a driveway are removed (a note that it is 'by others' stays)."""
    gone = {s.tag.strip().upper() for s in sheet.schedule if _DRIVEWAY.search(s.text)}
    before = len(sheet.elements) + len(sheet.schedule) + len(sheet.notes)
    sheet.schedule = [s for s in sheet.schedule if s.tag.strip().upper() not in gone]
    sheet.elements = [e for e in sheet.elements
                      if e.tag.strip().upper() not in gone
                      and not (e.kind in ("callout", "tag", "cloud") and _DRIVEWAY.search(e.text or ""))]
    sheet.notes = [n for n in sheet.notes if not _DRIVEWAY.search(n) or _BY_OTHERS.search(n)]
    removed = before - (len(sheet.elements) + len(sheet.schedule) + len(sheet.notes))
    return [f"{title}: {removed} driveway item(s) removed (driveways are not marked up)"] if removed else []


def ledger_tags(sheet: MarkupSheet, title: str) -> list[str]:
    """Office practice: ledgers are tagged LD, never L (lintels only). A ledger scheduled under
    an L tag ('L5: 140x45 F17/LVL LEDGER') is re-tagged LD with the same number where free
    (L5 -> LD5), everywhere on the sheet: members, schedule, callouts and notes."""
    used = {s.tag.strip().upper() for s in sheet.schedule} | {e.tag.strip().upper() for e in sheet.elements}
    renames: dict[str, str] = {}
    for item in sheet.schedule:
        tag = item.tag.strip().upper()
        m = re.fullmatch(r"L(\d+)", tag)
        if not m or not re.search(r"\bLEDGER", item.text, re.I) or re.search(r"\bLINTEL", item.text, re.I):
            continue
        new, n = f"LD{m.group(1)}", int(m.group(1))
        while new in used:
            n += 1
            new = f"LD{n}"
        used.add(new)
        renames[tag] = new
    if not renames:
        return []
    rx = re.compile(r"\b(" + "|".join(map(re.escape, renames)) + r")\b", re.I)

    def sub(text: str) -> str:
        return rx.sub(lambda m: renames[m.group(1).upper()], text or "")

    sheet.schedule = [s.model_copy(update={"tag": renames.get(s.tag.strip().upper(), s.tag)}) for s in sheet.schedule]
    sheet.elements = [e.model_copy(update={"tag": renames.get(e.tag.strip().upper(), e.tag), "text": sub(e.text)})
                      for e in sheet.elements]
    sheet.notes = [sub(n) for n in sheet.notes]
    return [f"{title}: ledger tag(s) {', '.join(f'{a} -> {b}' for a, b in renames.items())} "
            "(ledgers are tagged LD; L is for lintels)"]


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
    result.sheet_warnings[kind] += ledger_tags(sheet, SHEET_TITLES[kind])
    result.sheet_warnings[kind] += drop_driveways(sheet, SHEET_TITLES[kind])
    if kind in (SheetKind.FOUNDATION, SheetKind.FLOOR):
        result.sheet_warnings[kind] += wet_area_on_slab(result.design, sheet, SHEET_TITLES[kind])
        try:
            pg = next((p for p in pages if getattr(p, "number", None) == sheet.source_page_number), None)
            aspect = (pg.width / pg.height) if pg is not None and getattr(pg, "width", 0) else 1.414
        except Exception:
            aspect = 1.414
        result.sheet_warnings[kind] += engaged_piers(sheet, SHEET_TITLES[kind], aspect)
    # House style: schedules list only the member type and spacing - no notes.
    sheet.schedule = [s.model_copy(update={"text": short_spec(s.text)}) for s in sheet.schedule]
    result.sheet_warnings[kind] += stock.check(sheet.schedule, SHEET_TITLES[kind])
    sheet.legend = [s.model_copy(update={"text": short_spec(s.text)}) for s in sheet.legend]
    # Australian English on everything written on the sheet (catches American spellings).
    # Units in their SI case (450 mm, 27 kN, 0.8 kPa) and no value cited 'from memory'.
    sheet.notes = [spelling.tidy(n) for n in sheet.notes]
    sheet.classification = spelling.tidy(sheet.classification)
    sheet.elements = [e.model_copy(update={"text": spelling.tidy(e.text)}) for e in sheet.elements]
    sheet.schedule = [s.model_copy(update={"text": spelling.tidy(s.text)}) for s in sheet.schedule]
    sheet.legend = [s.model_copy(update={"text": spelling.tidy(s.text)}) for s in sheet.legend]
    result.planned[kind] = sheet
    result.sheets[kind] = sheet
    result.sheet_errors.pop(kind, None)


_SECTION = re.compile(r"(?:\d/)?\d{2,3}X\d{1,3}(?:\.\d+)?|\d{3}\s?PFC|\d{3}UB\d+(?:\.\d+)?")
_SAME_MEMBER = {"VB": "ROOFBEAM", "RB": "ROOFBEAM"}  # tag prefixes that name the same kind of member


def _retag(sheet: MarkupSheet, renames: dict[str, str]) -> None:
    rx = re.compile(r"(?<![A-Z0-9])(" + "|".join(map(re.escape, renames)) + r")(?![A-Z0-9])", re.I)

    def sub(text: str) -> str:
        return rx.sub(lambda m: renames[m.group(1).upper()], text or "")

    sheet.schedule = [s.model_copy(update={"tag": renames.get(s.tag.strip().upper(), s.tag)}) for s in sheet.schedule]
    sheet.elements = [e.model_copy(update={"tag": renames.get(e.tag.strip().upper(), e.tag), "text": sub(e.text)})
                      for e in sheet.elements]
    sheet.notes = [sub(n) for n in sheet.notes]


def consistent_tags(sheets: dict[SheetKind, MarkupSheet]) -> list[str]:
    """One tag per member across the set: a deck / verandah roof beam scheduled as VB1 on one
    sheet and RB1 (same section) on another takes the tag of the first sheet (roof before
    walls before footings) everywhere. Returns warnings naming each change."""
    first: dict[tuple[str, frozenset], str] = {}  # (member kind, sections) -> tag
    out = []
    for kind in SheetKind:
        sheet = sheets.get(kind)
        if sheet is None:
            continue
        renames: dict[str, str] = {}
        for item in sheet.schedule:
            tag = item.tag.strip().upper()
            m = re.match(r"([A-Z]+)\d+$", tag)
            group = _SAME_MEMBER.get(m.group(1)) if m else None
            sections = frozenset(_SECTION.findall(item.text.upper()))
            if not group or not sections:
                continue
            key = (group, sections)
            if key in first and first[key] != tag:
                renames[tag] = first[key]
            else:
                first.setdefault(key, tag)
        used = {s.tag.strip().upper() for s in sheet.schedule} - set(renames)
        renames = {a: b for a, b in renames.items() if b not in used}  # never merge two members
        if renames:
            _retag(sheet, renames)
            out.append(f"{SHEET_TITLES[kind]}: tag(s) {', '.join(f'{a} -> {b}' for a, b in renames.items())} "
                       "to match the other sheets (one tag per member)")
    return out


def _build(job: JobInput, result: JobResult) -> None:
    result.sheets = {k: result.planned[k].model_copy(deep=True) for k in SheetKind if k in result.planned}
    # The engineer's own notes go on every sheet word for word (after the rules, so they are
    # never rewritten), skipping any Claude already wrote. Added here, at build time, so notes
    # changed while the review was paused reach every sheet.
    for sheet in result.sheets.values():
        have = {_norm(n) for n in sheet.notes}
        sheet.notes += [_as_note(n) for n in job.sheet_notes if _norm(n) not in have]
    retagged = consistent_tags(result.sheets)
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
        calc_sheet=job.calc_sheet,
    )
    result.build_warnings = retagged + result.build_warnings
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
