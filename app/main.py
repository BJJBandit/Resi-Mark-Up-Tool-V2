"""Web front end: upload documents, run the review in the background, download results."""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import pipeline
from .schemas import SHEET_TITLES

app = FastAPI(title="Resi Mark-Up Tool")

INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text()
MAX_UPLOAD = 30 * 1024 * 1024  # Claude request limit is 32 MB


@dataclass
class Job:
    status: str = "queued"  # queued | running | done | error
    step: str = ""
    error: str | None = None
    project: str = ""
    result: pipeline.JobResult | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


JOBS: dict[str, Job] = {}


async def _read(f: UploadFile | None, name: str) -> bytes | None:
    if f is None or not f.filename:
        return None
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{name} is larger than 30 MB.")
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
) -> dict:
    arch = await _read(architectural, "Architectural drawings")
    if arch is None:
        raise HTTPException(400, "Architectural drawings are required.")
    inp = pipeline.JobInput(
        project=project.strip(),
        arch_pdf=arch,
        geotech_pdf=await _read(geotech, "Geotechnical report"),
        wind_class=wind_class.strip() or None,
        site_class=site_class.strip() or None,
        construction=construction.strip(),
        notes=notes.strip(),
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
        body["sheet_errors"] = {SHEET_TITLES[k]: v for k, v in job.result.sheet_errors.items()}
        body["summary"] = {
            "site_class": job.result.geotech.site_classification,
            "wind_class": job.result.geotech.wind_classification,
            "roof": job.result.design.roof.roof_form,
            "walls": job.result.design.walls.external_wall_type,
            "footing": job.result.design.footing.system,
            "floor": job.result.design.floor.system,
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
                }
            )
        )
    )
