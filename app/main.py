"""Web app: two-stage pipeline.

Stage 1  POST /jobs           upload → extract geometry → label (Claude) → status "review"
         GET  /jobs/{id}/review   the model review page; GET/PUT /jobs/{id}/model
Stage 2  PUT  /jobs/{id}/model?approve=true → plan → engines → layout → sheets → PDF
         GET  /jobs/{id}/markup.pdf | /trace | /trace.md | /trace.mmd | /review.json

The v1 all-Claude pipeline is still reachable with form field `mode=legacy` as the
"eyeball" fallback for image-only drawing sets.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import pipeline
from .design.annotate import annotate, page_sizes_from_pdf
from .design.runner import DesignRun, run_design
from .engines.prefs import load_preferences
from .label import UsageTotals, label_project
from .label.plan_image import labelled_plan_png
from .model import Project, readiness
from .pdf import build_sheets_pdf
from .routes_logic import router as logic_router
from .schemas import SHEET_TITLES

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Resi Mark-Up Tool")
app.include_router(logic_router)

STATIC = Path(__file__).parent / "static"
INDEX_HTML = (STATIC / "index.html").read_text()
REVIEW_HTML = (STATIC / "review.html").read_text()
TRACE_HTML = (STATIC / "trace.html").read_text()
MAX_UPLOAD = 30 * 1024 * 1024
MAX_REF_UPLOAD = 100 * 1024 * 1024
MAX_REF_PAGES = 500


@dataclass
class Job:
    status: str = "queued"  # queued | extracting | review | designing | done | error
    step: str = ""
    error: str | None = None
    project_name: str = ""
    arch_pdf: bytes = b""
    geotech_pdf: bytes | None = None
    options: dict[str, Any] = field(default_factory=dict)
    model: Project | None = None
    usage: UsageTotals = field(default_factory=UsageTotals)
    extract_diagnostics: dict[str, Any] = field(default_factory=dict)
    run: DesignRun | None = None
    sheets: list = field(default_factory=list)
    pdf: bytes = b""
    warnings: list[str] = field(default_factory=list)
    legacy: pipeline.JobResult | None = None
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


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------


def _extract_worker(job_id: str) -> None:
    job = JOBS[job_id]
    try:
        from .extract import extract_project  # deterministic geometry stage

        job.status = "extracting"
        job.step = "Reading vector geometry, dimensions and scale"
        project, diagnostics = extract_project(job.arch_pdf)
        job.extract_diagnostics = diagnostics if isinstance(diagnostics, dict) else {"diagnostics": diagnostics}
        o = job.options
        project.project_name = o.get("project") or project.project_name
        project.job_no = o.get("job_no", "")
        project.construction_nominated = o.get("construction", "")
        project.engineer_notes = o.get("notes", "")
        if o.get("wind_class"):
            project.site.wind.stated_class = o["wind_class"]
            project.site.wind.stated_source = "user"
        if o.get("site_class"):
            project.site.geotech.site_class = o["site_class"]
        job.step = "Labelling: roof text, load-bearing walls, geotech report"
        label_project(project, job.arch_pdf, job.geotech_pdf, job.usage)
        if not any(p.is_vector for p in project.pages):
            project.review.needs_attention.insert(0, "No vector data in the drawing set - use the legacy (image) mode or request CAD PDFs")
        job.model = project
        job.status = "review"
        job.step = "Model ready for review"
    except Exception as e:
        traceback.print_exc()
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"


def _legacy_worker(job_id: str, inp: pipeline.JobInput) -> None:
    job = JOBS[job_id]
    job.status = "designing"

    def progress(msg: str) -> None:
        job.step = msg

    try:
        job.legacy = pipeline.run(inp, progress)
        job.pdf = job.legacy.pdf
        job.warnings = job.legacy.warnings
        job.status = "done"
        job.step = "Complete (legacy mode)"
    except Exception as e:
        traceback.print_exc()
        job.status = "error"
        job.error = str(e)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/jobs")
async def create_job(
    architectural: UploadFile = File(...),
    geotech: UploadFile | None = File(None),
    project: str = Form(""),
    wind_class: str = Form(""),
    site_class: str = Form(""),
    construction: str = Form("Timber frame to AS 1684"),
    notes: str = Form(""),
    references: list[UploadFile] = File([]),
    title_block: bool = Form(False),
    job_no: str = Form(""),
    rev: str = Form("A"),
    made_by: str = Form(""),
    mode: str = Form("model"),
) -> dict:
    arch = await _read(architectural, "Architectural drawings")
    if arch is None:
        raise HTTPException(400, "Architectural drawings are required.")
    geo = await _read(geotech, "Geotechnical report")
    job_id = uuid.uuid4().hex
    options = dict(project=project.strip(), wind_class=wind_class.strip().upper() or None, site_class=site_class.strip().upper() or None,
                   construction=construction.strip(), notes=notes.strip(), title_block=title_block, job_no=job_no.strip(),
                   rev=rev.strip() or "A", made_by=made_by.strip().upper())
    job = Job(project_name=options["project"], arch_pdf=arch, geotech_pdf=geo, options=options)
    JOBS[job_id] = job

    if mode == "legacy":
        refs: list[tuple[str, bytes]] = []
        ref_pages = 0
        for f in references:
            data = await _read(f, f"Reference '{f.filename}'", MAX_REF_UPLOAD)
            if data is None:
                continue
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                ref_pages += doc.page_count
            refs.append((f.filename or "reference.pdf", data))
        if ref_pages > MAX_REF_PAGES:
            raise HTTPException(413, f"Reference documents total {ref_pages} pages; the limit is {MAX_REF_PAGES}.")
        inp = pipeline.JobInput(project=options["project"], arch_pdf=arch, geotech_pdf=geo, wind_class=options["wind_class"],
                                site_class=options["site_class"], construction=options["construction"], notes=options["notes"],
                                references=refs, title_block=title_block, job_no=options["job_no"], rev=options["rev"], made_by=options["made_by"])
        threading.Thread(target=_legacy_worker, args=(job_id, inp), daemon=True).start()
    else:
        threading.Thread(target=_extract_worker, args=(job_id,), daemon=True).start()
    return {"id": job_id, "mode": mode}


def _job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found (the server may have restarted).")
    return job


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _job(job_id)
    body: dict[str, Any] = {"status": job.status, "step": job.step, "error": job.error, "warnings": job.warnings}
    if job.model is not None:
        body["needs_attention"] = job.model.review.needs_attention
        body["readiness"] = [i.value for i in readiness(job.model)]
        body["usage"] = job.usage.as_dict()
    if job.run is not None:
        body["design"] = job.run.summary()
    if job.legacy is not None:
        r = job.legacy
        body["sheet_errors"] = {SHEET_TITLES[k]: v for k, v in r.sheet_errors.items()}
        body["summary"] = {"site_class": r.geotech.site_classification, "wind_class": r.geotech.wind_classification,
                           "footing": r.design.footing_system, "engineer_checks": r.design.engineer_checks}
    return body


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
def review_page(job_id: str) -> str:
    _job(job_id)
    return REVIEW_HTML


@app.get("/jobs/{job_id}/model")
def get_model(job_id: str) -> JSONResponse:
    job = _job(job_id)
    if job.model is None:
        raise HTTPException(409, f"Model not ready (status: {job.status}).")
    body = json.loads(job.model.model_dump_json())
    body["_usage"] = job.usage.as_dict()
    body["_diagnostics"] = job.extract_diagnostics
    return JSONResponse(body)


@app.put("/jobs/{job_id}/model")
def put_model(job_id: str, body: dict, approve: bool = False) -> JSONResponse:
    job = _job(job_id)
    if job.status == "designing":
        raise HTTPException(409, "Design is running; wait for it to finish.")
    body.pop("_usage", None)
    body.pop("_diagnostics", None)
    try:
        project = Project.model_validate(body)
    except Exception as e:
        raise HTTPException(422, f"Model did not validate: {e}")
    if approve:
        blockers = readiness(project)
        if blockers:
            raise HTTPException(409, "Model not ready for design: " + ", ".join(i.value for i in blockers))
        project.review.status = "reviewed"
        project.review.reviewed_by = job.options.get("made_by", "")
    job.model = project
    if approve:
        threading.Thread(target=_design_worker, args=(job_id,), daemon=True).start()
    return get_model(job_id)


@app.get("/jobs/{job_id}/plan/{building_id}/{storey_id}.png")
def plan_image(job_id: str, building_id: str, storey_id: str) -> Response:
    job = _job(job_id)
    if job.model is None:
        raise HTTPException(409, "Model not ready.")
    b = job.model.building(building_id)
    storey = next((s for s in b.storeys if s.id == storey_id), None)
    if storey is None or storey.frame is None:
        raise HTTPException(404, "Storey or plan frame not found.")
    return Response(labelled_plan_png(job.arch_pdf, storey), media_type="image/png")


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------


def _design_worker(job_id: str) -> None:
    job = JOBS[job_id]
    job.status = "designing"
    try:
        assert job.model is not None
        prefs = load_preferences()
        job.step = "Running design engines"
        job.run = run_design(job.model, prefs=prefs)
        job.step = "Placing members and building sheets"
        job.sheets = annotate(job.model, job.run, page_sizes_from_pdf(job.arch_pdf), prefs)
        o = job.options
        job.pdf, pdf_warnings = build_sheets_pdf(job.arch_pdf, job.model.project_name or job.project_name,
                                                 [(s.title, s.sheet) for s in job.sheets], job_no=o.get("job_no", ""),
                                                 rev=o.get("rev", "A"), made_by=o.get("made_by", ""), title_block=bool(o.get("title_block")))
        job.warnings = [w for s in job.sheets for w in s.warnings] + pdf_warnings
        job.status = "done"
        job.step = "Complete"
    except Exception as e:
        traceback.print_exc()
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"


def _require_done(job: Job) -> None:
    if job.status != "done":
        raise HTTPException(409, f"Job not finished (status: {job.status}).")


@app.get("/jobs/{job_id}/markup.pdf")
def job_pdf(job_id: str) -> Response:
    job = _job(job_id)
    _require_done(job)
    name = (job.project_name or "markup").replace('"', "").replace(" ", "_")
    return Response(job.pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}_markup.pdf"'})


@app.get("/jobs/{job_id}/trace", response_class=HTMLResponse)
def trace_page(job_id: str) -> str:
    _job(job_id)
    return TRACE_HTML


@app.get("/jobs/{job_id}/trace.md", response_class=PlainTextResponse)
def trace_md(job_id: str) -> str:
    job = _job(job_id)
    if job.run is None:
        raise HTTPException(409, "Design has not run.")
    return job.run.trace.to_markdown()


@app.get("/jobs/{job_id}/trace.mmd", response_class=PlainTextResponse)
def trace_mmd(job_id: str) -> str:
    job = _job(job_id)
    if job.run is None:
        raise HTTPException(409, "Design has not run.")
    return job.run.trace.to_mermaid()


@app.get("/jobs/{job_id}/plan.mmd", response_class=PlainTextResponse)
def plan_mmd(job_id: str) -> str:
    job = _job(job_id)
    if job.run is None:
        raise HTTPException(409, "Design has not run.")
    return job.run.plan.mermaid()


@app.get("/jobs/{job_id}/review.json")
def job_json(job_id: str) -> JSONResponse:
    job = _job(job_id)
    if job.legacy is not None:
        r = job.legacy
        return JSONResponse(json.loads(json.dumps({"mode": "legacy", "geotech": r.geotech.model_dump(), "design": r.design.model_dump(),
                                                    "sheets": {k.value: v.model_dump() for k, v in r.sheets.items()}, "warnings": r.warnings})))
    if job.model is None:
        raise HTTPException(409, "Nothing to report yet.")
    body: dict[str, Any] = {"mode": "model", "model": json.loads(job.model.model_dump_json()), "usage": job.usage.as_dict(),
                            "extract_diagnostics": job.extract_diagnostics, "warnings": job.warnings}
    if job.run is not None:
        body["design"] = job.run.summary()
        body["trace"] = json.loads(job.run.trace.to_json())
        body["plan"] = [{"id": s.id, "engine": s.engine, "scope": s.scope, "skipped": s.skipped, "reason": s.reason} for s in job.run.plan.steps]
        body["sheets"] = [{"building": s.building_id, "kind": s.kind, "title": s.title, "warnings": s.warnings} for s in job.sheets]
    return JSONResponse(body)
