"""The web app without Claude: password, saved reviews, stored standards, rebuilds."""

import base64

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import jobstore, pipeline, refstore
from app.schemas import Box, Element, MarkupSheet, Point, SheetKind


def _basic(password):
    return {"Authorization": "Basic " + base64.b64encode(f"anyone:{password}".encode()).decode()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(jobstore, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(refstore, "STORE_DIR", tmp_path / "refs")
    import app.main as main
    monkeypatch.setattr(main, "APP_PASSWORD", "")
    return TestClient(main.app)


def test_password_is_required_when_set(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "APP_PASSWORD", "s3cret")
    assert client.get("/").status_code == 401
    assert client.get("/references").status_code == 401
    assert client.get("/", headers=_basic("wrong")).status_code == 401
    assert client.get("/", headers=_basic("s3cret")).status_code == 200
    assert client.get("/healthz").status_code == 200  # Render's health check needs no password


def test_open_without_a_password(client):
    assert client.get("/").status_code == 200


def test_saved_review_is_listed_and_served_after_a_restart(client):
    job_id = "a" * 32
    jobstore.save(job_id, "12 Smith St", {"status": "done", "warnings": ["w"], "usage": {"cost_usd": 1.5},
                                           "rerunnable": [{"kind": "walls", "title": "WALL FRAMING PLAN", "made": True}]},
                  {"sheets": {}}, b"%PDF-1.4 test")
    (listed,) = client.get("/jobs").json()
    assert listed["project"] == "12 Smith St" and listed["sheets"] == ["WALL FRAMING PLAN"]
    status = client.get(f"/jobs/{job_id}").json()  # not in memory: comes from disk
    assert status["saved"] is True and status["rerunnable"] == [] and status["warnings"] == ["w"]
    pdf = client.get(f"/jobs/{job_id}/markup.pdf")
    assert pdf.content == b"%PDF-1.4 test" and "12_Smith_St_markup.pdf" in pdf.headers["content-disposition"]
    assert client.get(f"/jobs/{job_id}/review.json").json() == {"sheets": {}}
    assert client.post(f"/jobs/{job_id}/sheets/walls").status_code == 404  # no re-run from disk
    assert client.get("/jobs/" + "b" * 32).status_code == 404
    assert client.get("/jobs/..%2F..%2Fetc").status_code == 404


def test_only_the_newest_saved_reviews_are_kept(client, monkeypatch):
    monkeypatch.setattr(jobstore, "JOBS_KEEP", 3)
    for i in range(5):
        jobstore.save(f"{i:032x}", f"job {i}", {"status": "done"}, {}, b"%PDF")
    assert len(jobstore.list_saved()) == 3


def test_stored_standard_pdf_round_trip(client):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "AS TEST")
    data = doc.tobytes()
    key = refstore.doc_key(data)
    refstore.save_pdf(key, "AS Test.pdf", data, 1)
    (listed,) = client.get("/references").json()
    assert listed["has_pdf"] is True and listed["names"] == ["AS Test.pdf"]
    assert refstore.load_pdf(key)[1] == data
    assert client.delete(f"/references/{key}").json() == {"deleted": True}
    assert client.get("/references").json() == []


def test_rebuilding_the_pdf_does_not_change_the_planned_sheets():
    """A re-run rebuilds every sheet; building must start from the sheets as planned or
    check clouds / added arrows pile up."""
    doc = pymupdf.open()
    doc.new_page(width=1190, height=842)
    result = pipeline.JobResult(geotech=None, design=None)
    result.planned[SheetKind.FLOOR] = MarkupSheet(
        source_page_number=1, plan_region=Box(x1=0, y1=0, x2=1, y2=1), classification="", schedule_title="",
        schedule=[], legend_title="", legend=[], notes=[],
        elements=[Element(kind="member", tag="J1", start=Point(x=.2, y=.2), end=Point(x=.2, y=.6), text="")])
    job = pipeline.JobInput(project="", arch_pdf=doc.tobytes(), geotech_pdf=None, wind_class=None, site_class=None,
                            construction="", notes="")
    counts = []
    for _ in range(3):
        pipeline._build(job, result)
        counts.append((len(result.planned[SheetKind.FLOOR].elements), len(result.sheets[SheetKind.FLOOR].elements)))
    assert len(set(counts)) == 1 and counts[0][0] == 1
    assert result.pdf.startswith(b"%PDF")
