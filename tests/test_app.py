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


def test_pause_changes_material_and_sheets(monkeypatch):
    """Paused after step 2: a new material re-runs step 2; sheets added while paused before
    the PDF are drawn; sheet notes changed while paused reach every sheet."""
    from app import claude
    from app.schemas import DesignAssessment, GeotechReview
    from app.schemas import SheetKind as K
    calls = {"design": [], "sheets": []}
    monkeypatch.setattr(claude, "review_geotech", lambda *a, **k: GeotechReview.model_construct(
        recommendations=[], founding="", site_classification="M", wind_classification="N2"))

    def design(pages, geotech, construction, notes, refs):
        calls["design"].append(construction)
        return DesignAssessment.model_construct(information_requests=[], opening_crosscheck=[], dimension_crosscheck=[],
                                                assumptions=[], external_wall_type="", framing_material="", governing_standard="")
    monkeypatch.setattr(claude, "assess_design", design)

    def plan(kind, pages, geotech, design, refs, direction):
        calls["sheets"].append(kind)
        return MarkupSheet.model_construct(source_page_number=1, plan_region=Box(x1=0, y1=0, x2=1, y2=1), elements=[],
                                           schedule=[], legend=[], notes=["-CLAUDE NOTE."], classification="",
                                           schedule_title="", legend_title="")
    monkeypatch.setattr(claude, "plan_sheet", plan)
    monkeypatch.setattr(pipeline, "apply_rules", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "check_geotech", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "stock", type("S", (), {"check": staticmethod(lambda *a: [])}))
    built = {}
    monkeypatch.setattr(pipeline, "build_markup_pdf", lambda *a, **k: (built.update(sheets=a[4]) or b"%PDF", []))

    plan_changes = {"after step 2 (drawing review)": {"construction": "Light gauge steel frame to NASH Standard"},
                    "before the PDF is built": {"sheets": ["walls", "foundation"], "sheet_notes": ["ALL TIMBER H3."]}}
    seen = []
    job = pipeline.JobInput(project="x", arch_pdf=b"", geotech_pdf=None, wind_class=None, site_class=None,
                            construction="Timber frame to AS 1684", notes="", sheets=[K.WALLS])
    job.checkpoint = lambda stage: (seen.append(stage), plan_changes.pop(stage, None))[1]
    result = pipeline._run(job, [], [], lambda _: None)
    assert calls["design"] == ["Timber frame to AS 1684", "Light gauge steel frame to NASH Standard"]
    assert calls["sheets"] == [K.WALLS, K.FOUNDATION]
    assert set(result.planned) == {K.WALLS, K.FOUNDATION}
    assert all("-ALL TIMBER H3." in s.notes for s in built["sheets"].values())
    assert seen.count("before the PDF is built") == 2  # paused again after adding the sheet


def test_pause_and_resume_endpoints(client):
    import app.main as main
    from app.schemas import SheetKind as K
    job = main.Job(status="running")
    main.JOBS["p1"] = job
    assert client.get("/jobs/p1").json()["pausable"]
    assert client.post("/jobs/p1/pause").status_code == 200 and job.pause_requested
    assert client.post("/jobs/p1/resume", data={}).status_code == 409  # not paused yet
    job.status, job.paused_at, job.settings = "paused", "after step 2", {"notes": "", "sheets": ["walls"]}
    st = client.get("/jobs/p1").json()
    assert st["status"] == "paused" and st["settings"]["sheets"] == ["walls"]
    r = client.post("/jobs/p1/resume", data={"notes": "USE LVL", "construction": "Timber frame to AS 1684",
                                             "sheets": ["walls", "roof"], "sheet_notes": "A.\nB."})
    assert r.status_code == 200 and job.resumed.is_set()
    assert job.changes["sheets"] == [K.WALLS.value, K.ROOF.value] and job.changes["sheet_notes"] == ["A.", "B."]
    main.JOBS.pop("p1")


def test_stop_review(client, monkeypatch):
    """Stop releases a pause / wait and the runner ends as 'stopped' (nothing saved)."""
    import threading
    import time

    import app.main as main
    started = threading.Event()

    def fake_run(inp, progress):
        progress("Step 1")
        started.set()
        inp.checkpoint("after step 1")  # paused here
        progress("Step 2")
        raise AssertionError("should have stopped")

    monkeypatch.setattr(pipeline, "run", fake_run)
    job = main.Job()
    main.JOBS["s1"] = job
    job.pause_requested = True
    inp = pipeline.JobInput(project="x", arch_pdf=b"", geotech_pdf=None, wind_class=None, site_class=None,
                            construction="t", notes="")
    t = threading.Thread(target=main._worker, args=("s1", inp), daemon=True)
    t.start()
    started.wait(2)
    for _ in range(50):
        if job.status == "paused":
            break
        time.sleep(0.02)
    assert client.get("/jobs/s1").json()["stoppable"]
    assert client.post("/jobs/s1/stop").status_code == 200
    t.join(2)
    assert job.status == "stopped" and job.result is None
    assert client.post("/jobs/s1/stop").status_code == 409
    main.JOBS.pop("s1")


def test_office_rules_endpoint(client):
    from app import claude
    data = client.get("/office-rules").json()
    secs = data["sections"]
    assert any("Footings" in s["title"] for s in secs) and all(s["rules"] for s in secs)
    # intro + sections rebuild the file exactly as the page sends it back
    rebuilt = data["intro"] + "".join(f"\n\n## {s['title']}\n{s['body']}" for s in secs)
    assert " ".join(rebuilt.split()) == " ".join(claude.OFFICE_RULES.split())


def test_job_office_rules_used_in_prompt():
    import contextvars

    from app import claude, stock

    def inside():
        assert claude._system() == claude.SYSTEM  # defaults
        claude.set_office_rules("## Footings\n- Strip footings 600 wide for this job.")
        assert "600 wide for this job" in claude._system() and "600 wide" not in claude.SYSTEM
        claude.set_office_rules("")
        assert claude._system() == claude.SYSTEM
    contextvars.copy_context().run(inside)
    rules = "## Member sizes - office stock list\n- Timber, F17 / LVL: 90x35.\n"
    assert stock.check([type("I", (), {"tag": "L1", "text": "120x35 F17/LVL LINTEL"})()], "W", rules)


def _png(w=200, h=120):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), 0)
    pix.clear_with(200)
    return pix.tobytes("png")


def test_details_store_and_sheet(client, tmp_path, monkeypatch):
    from app import detailstore
    from app import pdf as P
    monkeypatch.setattr(detailstore, "DETAILS_DIR", tmp_path / "details")
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "PIER DETAIL")
    r = client.post("/details", files=[("files", ("Pier detail.png", _png(), "image/png")),
                                        ("files", ("Tie down.pdf", doc.tobytes(), "application/pdf"))])
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()]
    listed = client.get("/details").json()
    assert {d["name"] for d in listed} == {"Pier detail", "Tie down"}
    assert client.get(f"/details/{ids[0]}/thumb.png").status_code == 200
    assert client.post("/details", files=[("files", ("x.txt", b"hello", "text/plain"))]).status_code == 400
    client.post(f"/details/{ids[0]}/name", data={"name": "P1 PIER"})
    assert any(d["name"] == "P1 PIER" for d in client.get("/details").json())
    # the DETAILS sheet: 2 details on one page, captioned in order
    details = [detailstore.load(i) for i in ids]
    out = pymupdf.open()
    w = P._details_sheets(out, P.SheetMeta("J", "", "A", "", "01/01/2026"), details, 3)
    assert w == [] and out.page_count == 1
    caps = [a.info["content"] for a in out[0].annots() if a.info.get("subject") == "Detail Caption"]
    assert caps == ["DETAIL 1 - P1 PIER", "DETAIL 2 - TIE DOWN"]
    assert out[0].get_images() and "PIER DETAIL" in out[0].get_text()
    assert client.delete(f"/details/{ids[1]}").json()["deleted"] is True
    assert len(client.get("/details").json()) == 1


def test_details_one_tile_each_pdf_page_dxf_dwg(client, tmp_path, monkeypatch):
    import ezdxf

    from app import detailstore
    monkeypatch.setattr(detailstore, "DETAILS_DIR", tmp_path / "details")
    doc = pymupdf.open()
    for t in ("PIER", "FOOTING", "LINTEL"):
        doc.new_page().insert_text((50, 50), t)
    r = client.post("/details", files=[("files", ("Standard details.pdf", doc.tobytes(), "application/pdf"))])
    assert [d["name"] for d in r.json()] == ["Standard details - 1", "Standard details - 2", "Standard details - 3"]
    tiles = {d["name"]: d["id"] for d in client.get("/details").json()}
    name, data, kind = detailstore.load(tiles["Standard details - 2"])
    assert kind == "pdf" and pymupdf.open(stream=data).page_count == 1 and "FOOTING" in pymupdf.open(stream=data)[0].get_text()
    # DXF -> one vector tile
    dx = ezdxf.new()
    dx.modelspace().add_lwpolyline([(0, 0), (1000, 0), (1000, 600), (0, 600)], close=True)
    import io
    buf = io.StringIO()
    dx.write(buf)
    r = client.post("/details", files=[("files", ("Tie down.dxf", buf.getvalue().encode(), "application/dxf"))])
    assert r.status_code == 200 and r.json()[0]["kind"] == "pdf" and r.json()[0]["name"] == "Tie down"
    # DWG -> a clear message
    r = client.post("/details", files=[("files", ("Bracing.dwg", b"AC1032" + b"\0" * 100, "application/acad"))])
    assert r.status_code == 400 and "DXF" in r.json()["detail"]


def test_detail_split_suggest_and_cut(client, tmp_path, monkeypatch):
    from app import detailstore
    monkeypatch.setattr(detailstore, "DETAILS_DIR", tmp_path / "details")
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.draw_rect(pymupdf.Rect(15, 15, 827, 580))  # sheet frame: ignored
    for i, (x, y) in enumerate([(60, 60), (330, 60), (600, 60), (60, 330), (330, 330)]):
        page.draw_rect(pymupdf.Rect(x, y, x + 180, y + 150))
        page.insert_text((x + 10, y + 175), f"DETAIL {i + 1} - PIER TYPE {i + 1}", fontsize=10)
    key = client.post("/details", files=[("files", ("Sheet.pdf", doc.tobytes(), "application/pdf"))]).json()[0]["id"]
    assert client.get(f"/details/{key}/page.png").status_code == 200
    sg = client.get(f"/details/{key}/suggest").json()
    assert [r["name"] for r in sg["regions"]] == [f"DETAIL {i} - PIER TYPE {i}" for i in range(1, 6)]
    assert all(r["y1"] * 595 > (60 + 165 if r["y0"] < 0.3 else 330 + 165) for r in sg["regions"])  # title inside
    made = client.post(f"/details/{key}/split", json={"regions": sg["regions"][:2], "remove_original": True}).json()
    assert [m["name"] for m in made] == ["DETAIL 1 - PIER TYPE 1", "DETAIL 2 - PIER TYPE 2"]
    names = {d["name"] for d in client.get("/details").json()}
    assert names == {"DETAIL 1 - PIER TYPE 1", "DETAIL 2 - PIER TYPE 2"}  # original removed
    cut = detailstore.load(made[0]["id"])[1]
    p = pymupdf.open(stream=cut)[0]
    assert p.rect.width < 260 and "PIER TYPE 1" in p.get_text() and "PIER TYPE 2" not in p.get_text()
    # an image tile
    png = page.get_pixmap(dpi=100).tobytes("png")
    k2 = client.post("/details", files=[("files", ("Sheet.png", png, "image/png"))]).json()[0]["id"]
    regs = client.get(f"/details/{k2}/suggest").json()["regions"]
    assert len(regs) == 5
    assert client.post(f"/details/{k2}/split", json={"regions": []}).status_code == 400


def test_draft_pdf_while_running(client):
    import app.main as main
    from app.schemas import DesignAssessment, GeotechReview
    from app.schemas import SheetKind as K
    src = pymupdf.open()
    src.new_page(width=1190.55, height=841.89)
    inp = pipeline.JobInput(project="1 TEST ST", arch_pdf=src.tobytes(), geotech_pdf=None, wind_class=None,
                            site_class=None, construction="t", notes="", calc_sheet=False)
    res = pipeline.JobResult(geotech=GeotechReview.model_construct(), design=DesignAssessment.model_construct())
    job = main.Job(status="running", project="1 TEST ST")
    job.inp = inp
    main.JOBS["d1"] = job
    assert client.get("/jobs/d1").json()["draft_sheets"] == 0
    assert client.get("/jobs/d1/draft.pdf").status_code == 409  # nothing yet
    job.partial = res
    sh = MarkupSheet.model_construct(source_page_number=1, plan_region=Box(x1=0, y1=0, x2=1, y2=1), elements=[],
                                     schedule=[], legend=[], notes=["-A."], classification="", schedule_title="",
                                     legend_title="")
    res.planned[K.WALLS] = sh
    assert client.get("/jobs/d1").json()["draft_sheets"] == 1
    r = client.get("/jobs/d1/draft.pdf")
    assert r.status_code == 200 and "DRAFT_1_sheets" in r.headers["content-disposition"]
    doc = pymupdf.open(stream=r.content)
    assert doc.page_count == 2 and any(a.info.get("subject") == "Draft" for a in doc[0].annots())
    assert res.sheets == {} and res.pdf == b"" and sh.notes == ["-A."]  # the running review is untouched
    main.JOBS.pop("d1")


def test_answers_can_tick_stored_standards(client, monkeypatch, tmp_path):
    import app.main as main
    from app import refstore
    monkeypatch.setattr(refstore, "STORE_DIR", tmp_path)
    doc = pymupdf.open()
    doc.new_page()
    pdf = doc.tobytes()
    key = refstore.doc_key(pdf)
    refstore.save_pdf(key, "AS 1684.2 Table 8.18.pdf", pdf, 1)
    job = main.Job(status="waiting", questions=[r.model_dump() for r in _requests()])
    main.JOBS["w2"] = job
    r = client.post("/jobs/w2/answers", data={"answer_0": "yes", "stored_0": key, "answer_1": "no", "answer_2": "ignore"})
    assert r.status_code == 200
    assert job.answers[0]["files"] == [("AS 1684.2 Table 8.18.pdf", pdf)]
    job.answered.clear()
    job.answers = None
    r = client.post("/jobs/w2/answers", data={"answer_0": "yes", "stored_0": "f" * 64, "answer_1": "no", "answer_2": "ignore"})
    assert r.status_code == 400 and "no longer stored" in r.json()["detail"]
    main.JOBS.pop("w2")
