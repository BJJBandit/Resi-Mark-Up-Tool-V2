"""Web front end: upload documents, run the review in the background, download results."""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import pipeline, refstore
from .pdf import MAX_PAGES, check_pages, combine_pdfs, extract_pages, parse_page_range
from .refpages import MAX_REF_PAGES, RefDoc
from .schemas import SHEET_TITLES, SheetKind

app = FastAPI(title="Resi Mark-Up Tool")

INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text().replace("{{REF_PAGE_CAP}}", str(MAX_REF_PAGES))
MAX_UPLOAD = 30 * 1024 * 1024  # Claude request limit is 32 MB
MAX_GEOTECH = 22 * 1024 * 1024  # sent inline (base64, +33%) in one request: stay under 32 MB
MAX_REF_UPLOAD = 100 * 1024 * 1024  # references go via the Files API, not inline


@dataclass
class Job:
    status: str = "queued"  # queued | running | done | error
    step: str = ""
    error: str | None = None
    project: str = ""
    result: pipeline.JobResult | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


JOBS: dict[str, Job] = {}


async def _read(f: UploadFile | None, name: str, limit: int = MAX_UPLOAD) -> bytes | None:
    if f is None or not f.filename:
        return None
    data = await f.read()
    if len(data) > limit:
        raise HTTPException(413, f"{name} is larger than {limit // (1024 * 1024)} MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, f"{name} must be a PDF.")
    return data


def _worker(job_id: str, inp: pipeline.JobInput) -> None:
    job = JOBS[job_id]
    job.status = "running"

    def progress(msg: str) -> None:
        job.step = msg

    try:
        job.result = pipeline.run(inp, progress)
        job.status = "done"
        job.step = "Complete"
    except Exception as e:
        traceback.print_exc()
        job.status = "error"
        job.error = str(e)


def _rerun_worker(job_id: str, kind: SheetKind, change: str) -> None:
    job = JOBS[job_id]

    def progress(msg: str) -> None:
        job.step = msg

    try:
        pipeline.rerun_sheet(job.result, kind, change, progress)
    except Exception as e:  # never lose the finished review over a failed re-run
        traceback.print_exc()
        job.result.reruns.append({"sheet": SHEET_TITLES[kind], "note": change, "ok": False,
                                  "detail": f"re-run failed ({e})"})
    finally:
        job.step = "Complete"
        job.status = "done"
        job.lock.release()


@app.post("/jobs/{job_id}/sheets/{kind}")
def rerun_sheet(job_id: str, kind: str, change: str = Form("")) -> dict:
    """Re-run one sheet with an optional change request, reusing the review's
    geotech and design assessment (and its cache), then rebuild the PDF."""
    job = _job(job_id)
    try:
        sk = SheetKind(kind)
    except ValueError:
        raise HTTPException(404, f"Unknown sheet '{kind}'.")
    if job.status != "done" or not job.result:
        raise HTTPException(409, "The review is still running - wait for it to finish.")
    if not pipeline.can_rerun(job.result, sk):
        raise HTTPException(409, f"{SHEET_TITLES[sk]} can't be re-run for this review.")
    if not job.lock.acquire(blocking=False):
        raise HTTPException(409, "A sheet is already being re-run - wait for it to finish.")
    job.status, job.step = "running", f"Re-running {SHEET_TITLES[sk]}"
    threading.Thread(target=_rerun_worker, args=(job_id, sk, change.strip()), daemon=True).start()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/references")
def stored_references() -> list[dict]:
    """Standards / guides with stored text extracts (pages already read once)."""
    return [{k: v for k, v in d.items() if k != "pages"} for d in refstore.list_documents()]


@app.get("/references/{key}.txt", response_class=PlainTextResponse)
def stored_reference_text(key: str) -> str:
    try:
        text = refstore.document_text(key)
    except ValueError:
        text = None
    if text is None:
        raise HTTPException(404, "No stored extract for that document.")
    return text


@app.delete("/references/{key}")
def delete_stored_reference(key: str) -> dict:
    """Forget a document's extracts (e.g. after finding a transcription error);
    its pages are read from the PDF again next time."""
    try:
        return {"deleted": refstore.delete_document(key)}
    except ValueError:
        raise HTTPException(404, "No stored extract for that document.")


@app.post("/jobs")
async def create_job(
    architectural: list[UploadFile] = File([]),
    architectural_pages: list[str] = Form([]),
    geotech: list[UploadFile] = File([]),
    geotech_pages: list[str] = Form([]),
    project: str = Form(""),
    wind_class: str = Form(""),
    site_class: str = Form(""),
    construction: str = Form("Timber frame to AS 1684"),
    notes: str = Form(""),
    sheet_notes: str = Form(""),
    references: list[UploadFile] = File([]),
    reference_pages: list[str] = Form([]),
    stored_refs: list[str] = Form([]),  # keys of standards kept from earlier reviews
    stored_ref_pages: list[str] = Form([]),
    title_block: bool = Form(False),
    drawing_pages: str = Form(""),
    job_no: str = Form(""),
    rev: str = Form("A"),
    made_by: str = Form(""),
    sheets: list[str] = Form([]),
    combine_foundation: bool = Form(True),
) -> dict:
    try:
        chosen = [SheetKind(s) for s in sheets]
    except ValueError:
        raise HTTPException(400, "Unknown sheet selected.")
    # Architectural drawings: one or more sets, each with its own pages to review,
    # combined into one set (in upload order) for the review and the mark-up.
    parts: list[tuple[str, bytes, list[int]]] = []
    for i, f in enumerate(architectural):
        name = f.filename or f"drawings {i + 1}.pdf"
        data = await _read(f, f"Architectural drawings '{name}'")
        if data is None:
            continue
        spec = architectural_pages[i] if i < len(architectural_pages) else ""
        if not spec.strip() and len(architectural) == 1:
            spec = drawing_pages  # older single-set form field
        try:
            parts.append((name, data, check_pages(data, spec, f"'{name}'")))
        except ValueError as e:
            raise HTTPException(400, f"Architectural drawings '{name}': {e}")
        except Exception:
            raise HTTPException(400, f"Architectural drawings '{name}' could not be opened as a PDF.")
    if not parts:
        raise HTTPException(400, "Architectural drawings are required.")
    total = sum(len(p[2]) for p in parts)
    if total > MAX_PAGES:
        raise HTTPException(400, f"{total} drawing pages selected across the drawing sets; the limit is {MAX_PAGES}. "
                                 "Use each set's 'Pages to review' to pick the plans, elevations and sections.")
    arch, labels = combine_pdfs(parts)
    if len(arch) > MAX_UPLOAD * 3:
        raise HTTPException(413, "The selected drawing pages are too large together; select fewer pages.")

    # Geotechnical documents (report, addenda, borehole logs ...), each with optional pages.
    geo_docs: list[tuple[str, bytes]] = []
    for i, f in enumerate(geotech):
        name = f.filename or f"geotech {i + 1}.pdf"
        data = await _read(f, f"Geotechnical document '{name}'")
        if data is None:
            continue
        spec = (geotech_pages[i] if i < len(geotech_pages) else "").strip()
        if spec:
            try:
                with pymupdf.open(stream=data, filetype="pdf") as doc:
                    sel = parse_page_range(spec, doc.page_count, "document")
            except ValueError as e:
                raise HTTPException(400, f"Geotechnical document '{name}': {e}")
            data = extract_pages(data, sel)
        geo_docs.append((name.rsplit(".", 1)[0], data))
    if sum(len(d) for _, d in geo_docs) > MAX_GEOTECH:
        raise HTTPException(413, f"Geotechnical documents total more than {MAX_GEOTECH // (1024 * 1024)} MB; "
                                 "enter 'Pages to review' to send only the pages needed.")

    refs: list[RefDoc] = []
    manual_pages = 0
    # Standards: uploaded now, or ticked from the ones stored by earlier reviews.
    sources: list[tuple[str, bytes, str]] = []  # name, pdf, pages spec
    for i, f in enumerate(references):
        data = await _read(f, f"Reference '{f.filename}'", MAX_REF_UPLOAD)
        if data is not None:
            sources.append((f.filename or "reference.pdf", data, reference_pages[i] if i < len(reference_pages) else ""))
    for i, key in enumerate(stored_refs):
        got = refstore.load_pdf(key)
        if got is None:
            raise HTTPException(400, "A stored standard you ticked is no longer stored - upload it again.")
        sources.append((got[0], got[1], stored_ref_pages[i] if i < len(stored_ref_pages) else ""))
    seen: set[str] = set()
    for name, data, spec in sources:
        key = refstore.doc_key(data)
        if key in seen:  # the same standard uploaded and ticked: use it once
            continue
        seen.add(key)
        spec = spec.strip()
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                count = doc.page_count
        except Exception:
            raise HTTPException(400, f"Reference '{name}' could not be opened as a PDF.")
        if refstore.enabled():
            try:  # keep it, so later reviews can tick it instead of uploading it again
                refstore.save_pdf(key, name, data, count)
            except OSError as e:
                print(f"[refstore] could not store {name}: {e}", flush=True)
        if not spec:  # blank: pages chosen automatically from the contents during the review
            refs.append(RefDoc(name, data, count))
            continue
        try:
            sel = parse_page_range(spec, count, "document")
        except ValueError as e:
            raise HTTPException(400, f"Reference '{name}': {e}")
        manual_pages += len(sel)
        how = "whole document (all)" if len(sel) == count else "chosen on the form"
        refs.append(RefDoc(name, data, count, sel, how))
    if manual_pages > MAX_REF_PAGES:
        raise HTTPException(
            413,
            f"Reference documents total {manual_pages} selected pages; the limit is {MAX_REF_PAGES}. "
            "Leave a document's 'Pages to review' blank to pick the relevant sections from its contents, "
            "or enter a smaller range.",
        )
    inp = pipeline.JobInput(
        project=project.strip(),
        arch_pdf=arch,
        geotech_pdf=geo_docs or None,
        wind_class=wind_class.strip() or None,
        site_class=site_class.strip() or None,
        construction=construction.strip(),
        notes=notes.strip(),
        sheet_notes=[n.strip() for n in sheet_notes.splitlines() if n.strip()],
        references=refs,
        title_block=title_block,
        pages=[],  # arch holds only the selected pages
        page_labels=labels,
        job_no=job_no.strip(),
        rev=rev.strip() or "A",
        made_by=made_by.strip().upper(),
        sheets=chosen,
        combine_foundation=combine_foundation,
    )
    job_id = uuid.uuid4().hex
    JOBS[job_id] = Job(project=inp.project)
    threading.Thread(target=_worker, args=(job_id, inp), daemon=True).start()
    return {"id": job_id}


def _job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found (the server may have restarted).")
    return job


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _job(job_id)
    body: dict = {"status": job.status, "step": job.step, "error": job.error}
    if job.result:
        body["references"] = job.result.references
        body["reference_pages"] = job.result.reference_pages
        body["warnings"] = job.result.warnings
        body["usage"] = job.result.usage
        body["reruns"] = job.result.reruns
        body["rerunnable"] = [{"kind": k.value, "title": SHEET_TITLES[k], "made": k in job.result.sheets}
                              for k in SheetKind if pipeline.can_rerun(job.result, k)]
        body["dimension_crosscheck"] = [d.model_dump() for d in job.result.design.dimension_crosscheck]
        body["sheet_errors"] = {SHEET_TITLES[k]: v for k, v in job.result.sheet_errors.items()}
        body["summary"] = {
            "site_class": job.result.geotech.site_classification,
            "wind_class": job.result.geotech.wind_classification,
            "roof": f"{job.result.design.roof_form} - {job.result.design.roof_construction}",
            "walls": job.result.design.external_wall_type,
            "footing": job.result.design.footing_system,
            "floor": job.result.design.floor_system,
            "engineer_checks": job.result.design.engineer_checks,
        }
    return body


@app.get("/jobs/{job_id}/markup.pdf")
def job_pdf(job_id: str) -> Response:
    job = _job(job_id)
    if not job.result:
        raise HTTPException(409, "Job not finished.")
    name = (job.project or "markup").replace('"', "").replace(" ", "_")
    return Response(
        job.result.pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}_markup.pdf"'},
    )


@app.get("/jobs/{job_id}/review.json")
def job_json(job_id: str) -> JSONResponse:
    job = _job(job_id)
    if not job.result:
        raise HTTPException(409, "Job not finished.")
    r = job.result
    return JSONResponse(
        json.loads(
            json.dumps(
                {
                    "geotech": r.geotech.model_dump(),
                    "design": r.design.model_dump(),
                    "sheets": {k.value: v.model_dump() for k, v in r.sheets.items()},
                    "sheet_errors": {k.value: v for k, v in r.sheet_errors.items()},
                    "warnings": r.warnings,
                    "reference_pages": r.reference_pages,
                    "usage": r.usage,
                }
            )
        )
    )
