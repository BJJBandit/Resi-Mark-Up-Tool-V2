from pathlib import Path

import pytest

from app.design.annotate import annotate, page_sizes_from_pdf, sheet_notes, load_notes
from app.design.runner import run_design
from app.engines.prefs import load_preferences
from app.model import Building, Column, Floor, Geotech, Opening, PlanFrame, Project, Pt, Roof, Site, Storey, Wall, WindInputs

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "schouten_st_2024-C07-PR03" / "architectural.pdf"


def _project(site_class="M", floor="suspended_timber"):
    W, H = 14110.0, 11500.0
    pts = [Pt(x=0, y=0), Pt(x=W, y=0), Pt(x=W, y=H), Pt(x=0, y=H)]
    walls = [Wall(id=f"W{i}", start=pts[i], end=pts[(i + 1) % 4], thickness_mm=250, external=True, construction="brick_veneer",
                  load_bearing=True, openings=[Opening(id=f"o{i}", offset_mm=2000, width_mm=1800)]) for i in range(4)]
    walls.append(Wall(id="I1", start=Pt(x=7000, y=0), end=Pt(x=7000, y=H), thickness_mm=90, external=False, load_bearing=True))
    walls.append(Wall(id="I2", start=Pt(x=0, y=5500), end=Pt(x=W, y=5500), thickness_mm=90, external=False, load_bearing=False))
    st = Storey(id="g", name="ground", floor=Floor(kind=floor), walls=walls,
                columns=[Column(id="C1", at=Pt(x=W + 1500, y=0), section="89x3.5 SHS")],
                frame=PlanFrame(page_number=5, scale_denominator=100, origin=Pt(x=380, y=170)))
    b = Building(id="u2", name="Unit 2", external_wall_construction="brick_veneer",
                 roof=Roof(form="hip", construction="truss", pitch_deg=22.5, cladding="sheet", eaves_mm=450), storeys=[st])
    return Project(project_name="19 Schouten St", job_no="2024-C07-PR03",
                   site=Site(geotech=Geotech(report_provided=True, site_class=site_class, bearing_kpa=100),
                             wind=WindInputs(stated_class="N2", stated_source="architect")),
                   buildings=[b])


def test_runner_is_deterministic_and_records_pending_engines():
    p = _project()
    r1 = run_design(p)
    r2 = run_design(p)
    assert r1.trace.to_markdown().split("created")[0] == r2.trace.to_markdown().split("created")[0]
    assert r1.result("wind_as4055").wind_class == "N2"  # stated class used with a flag while Table 2.2 is empty
    f = r1.result("footings_as2870", "u2")
    assert f.footing_system == "strip_and_piers" and f.proposal["tag"] == "SF1" and not f.verified
    blocked = " ".join(r1.trace.blocked_on())
    assert "AS 2870:2011 Table 3.4" in blocked and "Table 8.18" in blocked  # racking pending
    assert not r1.errors


def test_notes_follow_conditions():
    p = _project()
    lib = load_notes()
    notes = sheet_notes("footing_floor", p, p.buildings[0], lib)
    assert any("MEMBER LAYOUT PLAN ONLY" in n for n in notes)
    assert any("ARTICULATE MASONRY" in n for n in notes)  # brick veneer
    assert all(n.startswith("-") and n == n.upper() for n in notes)
    p2 = _project(); p2.buildings[0].external_wall_construction = "timber_stud"
    for w in p2.buildings[0].storeys[0].walls:
        w.construction = "timber_stud"
    assert not any("ARTICULATE" in n for n in sheet_notes("footing_floor", p2, p2.buildings[0], lib))


def test_annotate_footing_sheet_from_results():
    p = _project()
    run = run_design(p)
    sheets = annotate(p, run, {5: (1190.55, 841.89)}, load_preferences())
    kinds = [s.kind for s in sheets]
    assert kinds == ["footing_floor", "wall", "roof"]
    foot = sheets[0].sheet
    assert foot.classification == "AS 2870:2011 SITE CLASSIFICATION: M"
    tags = [e.tag for e in foot.elements]
    assert tags.count("SF1") == 5  # 4 external + 1 load-bearing internal wall
    assert "P2" in tags and "P1" in tags  # piers along footings, P1 under the column
    assert any(e.kind == "callout" and "BORED PIER" in e.text for e in foot.elements)
    sched = {s.tag: s.text for s in foot.schedule}
    assert sched["SF1"].startswith("450 W X MIN. 450 DEEP STRIP FOOTING")
    assert "C1" in sched
    assert any("ENGINEER TO VERIFY AGAINST AS 2870" in n for n in foot.notes)
    assert any("NOTICE TO OWNER" in n for n in foot.notes)
    assert 0 <= foot.plan_region.x1 < foot.plan_region.x2 <= 1
    roof = sheets[2].sheet
    assert roof.classification.endswith("N2") and any(e.tag == "TR1" and e.kind == "span" for e in roof.elements)
    assert any("bracing and lintels pending" in w for w in sheets[1].warnings)


def test_slab_building_gets_raft_elements():
    p = _project(floor="slab_on_ground")
    run = run_design(p)
    foot = annotate(p, run, {5: (1190.55, 841.89)}, load_preferences())[0].sheet
    tags = {e.tag for e in foot.elements}
    assert "EB1" in tags and "IB1" in tags
    assert any(e.kind == "callout" and "THICK REINFORCED CONCRETE SLAB" in e.text for e in foot.elements)


@pytest.mark.skipif(not FIX.exists(), reason="fixture missing")
def test_pdf_builds_from_annotated_sheets(tmp_path):
    from app.pdf import build_sheets_pdf

    pdf = FIX.read_bytes()
    p = _project()
    run = run_design(p)
    sheets = annotate(p, run, page_sizes_from_pdf(pdf), load_preferences())
    out, warnings = build_sheets_pdf(pdf, p.project_name, [(s.title, s.sheet) for s in sheets], job_no=p.job_no)
    assert out[:5] == b"%PDF-"
    import pymupdf
    doc = pymupdf.open(stream=out, filetype="pdf")
    assert doc.page_count >= 3
    assert sum(len(list(pg.annots())) for pg in doc) > 20
    (tmp_path / "markup.pdf").write_bytes(out)
