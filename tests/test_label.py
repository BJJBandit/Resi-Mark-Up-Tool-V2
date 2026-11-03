from pathlib import Path

import pytest

from app.label.geotech import GeotechExtract, to_geotech
from app.label.plan_image import labelled_plan_png
from app.label.roof_text import apply_roof_text, scan_roof_text
from app.model import Building, PlanFrame, Project, Pt, Roof, Storey, Wall

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def test_roof_text_bay_rd_style():
    rt = scan_roof_text({
        11: "Colorbond roof, fascia & gutters @ 22.5° pitch Colour: TBC  Brick veneer  TP Deck framing",
        12: "Colorbond roof, fascia & gutters @ 22.5° pitch  Selected cement sheet cladding",
        13: "Colorbond roof, fascia & gutters @ 15° pitch",
        8: "Timber engineered trusses at 900 centres. 450 wide eaves to brick",
    })
    assert rt.pitch_deg == 22.5
    assert sorted(set(rt.pitches_deg)) == [15.0, 22.5]
    assert rt.cladding == "sheet"
    assert rt.construction == "truss"
    assert rt.eaves_mm == 450
    roof = Roof()
    set_ = apply_roof_text(roof, rt)
    assert roof.pitch_deg == 22.5 and roof.cladding == "sheet" and roof.construction == "truss"
    assert roof.provenance.source == "pdf_text" and len(set_) == 4


def test_roof_text_rada_rd_style():
    rt = scan_roof_text({5: "Colorbond Custom Orb roof @ 15° pitch over prefabricated timber trusses supplied and installed to manufacturers specifications"})
    assert rt.pitch_deg == 15.0 and rt.cladding == "sheet" and rt.construction == "truss"


def test_geotech_parsing_units():
    x = GeotechExtract(
        report_found=True,
        site_classification="Class M",
        ys="30 mm",
        bearing="100 kPa at 0.5 m",
        founding_depth="0.5 m",
        founding_material="stiff clay",
        rock_depth="0.2 – 0.85 m",
        fill="none",
        groundwater="not encountered",
        trees_or_abnormal_moisture="no",
        landslip_or_slope_hazard="yes - low hazard area",
        wind_classification="N2",
        recommendations=["found on clay below 500"],
        concerns=[],
    )
    g = to_geotech(x)
    assert g.site_class == "M"
    assert g.ys_mm == 30 and g.bearing_kpa == 100
    assert g.founding_depth_mm == 500 and g.rock_depth_mm == 850
    assert g.fill_present is False and g.landslip_or_slope_hazard is True


@pytest.mark.skipif(not (FIX / "schouten_st_2024-C07-PR03" / "architectural.pdf").exists(), reason="fixture missing")
def test_labelled_plan_renders():
    pdf = (FIX / "schouten_st_2024-C07-PR03" / "architectural.pdf").read_bytes()
    # A synthetic frame: 1:100 on A3, origin near the plan's lower-left on page 5.
    frame = PlanFrame(page_number=5, scale_denominator=100, origin=Pt(x=380, y=170))
    storey = Storey(id="g", frame=frame, walls=[
        Wall(id="W1", start=Pt(x=0, y=0), end=Pt(x=14110, y=0), thickness_mm=250, external=True),
        Wall(id="W2", start=Pt(x=0, y=0), end=Pt(x=0, y=11500), thickness_mm=250, external=True),
        Wall(id="W3", start=Pt(x=7000, y=0), end=Pt(x=7000, y=6000), thickness_mm=90, external=False),
    ])
    png = labelled_plan_png(pdf, storey)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20_000
    Project(buildings=[Building(id="u2", storeys=[storey])])  # model accepts it
