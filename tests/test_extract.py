"""Regression tests for the deterministic geometry extraction against the fixture
architectural sets. Each test skips when its PDF is not present (the PDFs are not
in git - see fixtures/README.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extract import extract_project

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# job id -> (floor plan page, expected building id, storey, extents x, extents y)
KNOWN = {
    "bay_rd_2024-C07-PR01": dict(page=8, building="unit1", storey="ground", x=10080, y=12280),
    "schouten_st_2024-C07-PR03": dict(page=5, building="unit2", storey="ground", x=14110, y=11500),
    "rada_rd_2024-C19-PR01": dict(page=7, building="house", storey="ground", x=30790, y=21080),
}

_cache: dict[str, tuple] = {}


def _load(job: str):
    pdf = FIXTURES / job / "architectural.pdf"
    if not pdf.exists():
        pytest.skip(f"fixture {pdf} not present")
    if job not in _cache:
        _cache[job] = extract_project(pdf.read_bytes())
    return _cache[job]


def _plan_page(diag: dict, page: int) -> dict:
    for p in diag["pages"]:
        if p["page"] == page:
            return p
    raise AssertionError(f"page {page} missing from diagnostics")


@pytest.mark.parametrize("job", sorted(KNOWN))
def test_scale_found_and_confirmed(job):
    project, diag = _load(job)
    k = KNOWN[job]
    page = _plan_page(diag, k["page"])
    assert page["scale_used"] is not None, "no scale chosen"
    assert abs(page["scale_used"] - 100) < 1
    assert page["dimensions"]["agree"] >= 6, "too few dimension strings confirmed the scale"
    assert abs(page["dimensions"]["measured_scale"] - 100) / 100 < 0.02
    info = next(p for p in project.pages if p.number == k["page"])
    assert info.kind == "floor_plan"
    assert info.scale_denominator == pytest.approx(100, abs=1)


@pytest.mark.parametrize("job", sorted(KNOWN))
def test_overall_extents(job):
    project, diag = _load(job)
    k = KNOWN[job]
    page = _plan_page(diag, k["page"])
    # from the dimension strings
    ext = page["dimensions"]["extents_mm"]
    assert ext["x"] == pytest.approx(k["x"], rel=0.02)
    assert ext["y"] == pytest.approx(k["y"], rel=0.02)
    # from the wall geometry of the main plan
    plan = max(page["walls"]["plans"], key=lambda p: p["walls"])
    assert plan["extents_mm"]["x"] == pytest.approx(k["x"], rel=0.02)
    assert plan["extents_mm"]["y"] == pytest.approx(k["y"], rel=0.02)


@pytest.mark.parametrize("job", sorted(KNOWN))
def test_external_walls_closed(job):
    project, diag = _load(job)
    k = KNOWN[job]
    building = project.building(k["building"])
    storey = next(s for s in building.storeys if s.name == k["storey"])
    assert storey.frame is not None and storey.frame.page_number == k["page"]
    ext = storey.external_walls()
    assert len(ext) >= 4
    assert all(w.load_bearing is True for w in ext)
    assert all(w.load_bearing is None for w in storey.walls if not w.external)
    plan = max(_plan_page(diag, k["page"])["walls"]["plans"], key=lambda p: p["walls"])
    assert plan["closed"], "external walls do not form a closed boundary"
    # building-local frame: coordinates are mm, x right, y up, from the plan's corner
    xs = [p for w in storey.walls for p in (w.start.x, w.end.x)]
    ys = [p for w in storey.walls for p in (w.start.y, w.end.y)]
    assert min(xs) >= -200 and min(ys) >= -200
    assert max(xs) == pytest.approx(k["x"], rel=0.03)
    assert max(ys) == pytest.approx(k["y"], rel=0.03)
    assert building.footprint is not None and len(building.footprint.points) >= 4


@pytest.mark.parametrize("job", sorted(KNOWN))
def test_openings_found(job):
    project, _ = _load(job)
    k = KNOWN[job]
    storey = next(s for s in project.building(k["building"]).storeys if s.name == k["storey"])
    openings = [o for w in storey.walls for o in w.openings]
    assert len(openings) >= 8
    tagged = [o for o in openings if o.tag]
    assert len(tagged) >= 6, "most openings should carry an architect's tag"
    assert any(o.kind == "window" and o.width_mm >= 600 for o in tagged)
    for o in openings:
        assert 0 <= o.offset_mm
        assert o.width_mm > 0


def test_bay_rd_unit3_two_storeys():
    project, diag = _load("bay_rd_2024-C07-PR01")
    unit3 = project.building("unit3")
    names = sorted(s.name for s in unit3.storeys)
    assert names == ["first", "ground"]
    page = _plan_page(diag, 10)
    labels = sorted(p["label"] for p in page["walls"]["plans"])
    assert labels == ["FIRST FLOOR", "GROUND FLOOR"]
    assert {b.id for b in project.buildings} >= {"unit1", "unit2", "unit3"}


def test_rada_rd_title_block_site_data():
    project, _ = _load("rada_rd_2024-C19-PR01")
    assert project.site.geotech.site_class == "S"
    assert project.site.wind.stated_class == "N3"
    assert project.site.wind.stated_source == "architect"
    assert project.site.bal == "29"
    assert project.site.corrosion_environment == "medium"
    assert project.site.provenance["geotech.site_class"].source == "pdf_text"
    assert project.site.provenance["geotech.site_class"].page == 1
    cover = next(p for p in project.pages if p.number == 1)
    assert cover.kind == "cover"
    assert next(p for p in project.pages if p.number == 7).sheet_size == "A2"


def test_maveric_project_text():
    project, _ = _load("bay_rd_2024-C07-PR01")
    assert project.job_no == "J2109"
    assert "BAY ROAD" in project.project_name.upper()
    assert project.client.upper().startswith("IMPACT")
    assert project.site.geotech.site_class == "Unknown"  # not printed on these drawings
    assert any("site class" in n.lower() for n in project.review.needs_attention)


@pytest.mark.parametrize("job", sorted(KNOWN))
def test_review_flags_internal_walls(job):
    project, _ = _load(job)
    notes = project.review.needs_attention
    assert any("load-bearing" in n for n in notes)
    assert all(p.is_vector for p in project.pages if p.kind == "floor_plan")
