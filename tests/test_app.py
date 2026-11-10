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


def _requests():
    from app.schemas import InfoRequest
    return [InfoRequest(item="Truss layout", kind="document", why="girder positions", assumption="no girders over openings"),
            InfoRequest(item="BAL report", kind="document", why="deck material", assumption="BAL-12.5"),
            InfoRequest(item="Ceiling height", kind="information", why="stud height", assumption="2400")]


def test_information_answers_yes_no_ignore(monkeypatch):
    """Yes (typed) re-runs step 2 with it; No records the assumption; Ignore records nothing."""
    from app import claude
    from app.schemas import DesignAssessment
    calls = []

    def again(pages, geotech, construction, notes, refs):
        calls.append(notes)
        return DesignAssessment.model_construct(information_requests=_requests()[:1], assumptions=[])

    monkeypatch.setattr(claude, "assess_design", again)
    job = pipeline.JobInput(project="x", arch_pdf=b"", geotech_pdf=None, wind_class=None, site_class=None,
                            construction="timber", notes="")
    asked = []
    job.ask = lambda qs: asked.append(qs) or [{"answer": "ignore"}, {"answer": "no"},
                                              {"answer": "yes", "text": "2700 ceilings"}]
    design = DesignAssessment.model_construct(information_requests=_requests(), assumptions=[])
    out, warnings = pipeline._information(job, [], [], None, design, lambda _: None, {"text_refs": [], "files": []})
    assert [q["item"] for q in asked[0]] == ["Truss layout", "BAL report", "Ceiling height"]
    assert len(calls) == 1 and "2700 ceilings" in calls[0] and "BAL report is NOT available" in calls[0]
    assert "Truss layout" not in job.notes  # ignored: not on the sheets
    assert any("BAL-12.5" in a for a in out.assumptions)
    assert any("ignored: Truss layout" in w for w in warnings)
    assert [r.item for r in out.information_requests] == ["Truss layout", "BAL report", "Ceiling height"]


def test_information_unanswered_means_no(monkeypatch):
    from app import claude
    from app.schemas import DesignAssessment
    monkeypatch.setattr(claude, "assess_design", lambda *a: pytest.fail("step 2 re-run without new information"))
    job = pipeline.JobInput(project="x", arch_pdf=b"", geotech_pdf=None, wind_class=None, site_class=None,
                            construction="timber", notes="SLAB ON GROUND")
    design = DesignAssessment.model_construct(information_requests=_requests(), assumptions=["A"])
    out, _ = pipeline._information(job, [], [], None, design, lambda _: None, {"text_refs": [], "files": []})
    assert job.notes.startswith("SLAB ON GROUND") and job.notes.count("NOT available") == 3
    assert len(out.assumptions) == 4


def test_answers_endpoint(client):
    import app.main as main
    job = main.Job(status="waiting", questions=[r.model_dump() for r in _requests()])
    main.JOBS["w1"] = job
    st = client.get("/jobs/w1").json()
    assert st["status"] == "waiting" and len(st["questions"]) == 3
    # Yes with nothing attached or typed is refused
    r = client.post("/jobs/w1/answers", data={"answer_0": "yes", "answer_1": "no", "answer_2": "ignore"})
    assert r.status_code == 400
    doc = pymupdf.open()
    doc.new_page()
    pdf = doc.tobytes()
    r = client.post("/jobs/w1/answers", data={"answer_0": "yes", "answer_1": "no", "answer_2": "ignore"},
                    files={"files_0": ("trusses.pdf", pdf, "application/pdf")})
    assert r.status_code == 200 and job.answered.is_set()
    assert job.answers[0]["files"][0][0] == "trusses.pdf" and job.answers[1]["answer"] == "no"
    main.JOBS.pop("w1")
    assert client.post("/jobs/nope/answers", data={}).status_code == 404
