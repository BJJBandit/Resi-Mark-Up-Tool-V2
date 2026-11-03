from app.design.layout import (
    BracingRules, LintelInstance, PanelType, assign_lintel_tags, place_bracing, place_piers, solid_segments,
)
from app.model import Opening, Pt, Storey, Wall


def _rect_walls(W=10000, H=8000, thk=90, ext=True):
    pts = [Pt(x=0, y=0), Pt(x=W, y=0), Pt(x=W, y=H), Pt(x=0, y=H)]
    return [Wall(id=f"W{i}", start=pts[i], end=pts[(i + 1) % 4], thickness_mm=thk, external=ext, load_bearing=True) for i in range(4)]


def test_piers_at_corners_and_max_spacing():
    walls = _rect_walls(W=10000, H=6000)
    piers = place_piers(walls, max_spacing_mm=3000)
    # corners: 4; 10 m sides need 3 intermediate each (ceil(10/3)=4 spaces), 6 m sides need 1 each
    assert len(piers) == 4 + 2 * 3 + 2 * 1
    xs = sorted(round(p.x) for p in piers if p.y == 0)
    assert xs == [0, 2500, 5000, 7500, 10000]


def test_piers_under_point_loads_deduped():
    walls = _rect_walls()
    piers = place_piers(walls, 3000, point_loads=[Pt(x=0, y=0), Pt(x=5000, y=4000)])
    assert sum(1 for p in piers if p.x == 0 and p.y == 0) == 1
    assert any(p.x == 5000 and p.y == 4000 for p in piers)


def test_solid_segments_skip_openings():
    w = Wall(id="W", start=Pt(x=0, y=0), end=Pt(x=10000, y=0), thickness_mm=90, external=True,
             openings=[Opening(id="o1", offset_mm=2000, width_mm=1500), Opening(id="o2", offset_mm=6000, width_mm=2400)])
    segs = solid_segments(w, min_length_mm=600)
    assert [(round(s.offset_mm), round(s.length_mm)) for s in segs] == [(0, 2000), (3500, 2500), (8400, 1600)]


TYPES = [
    PanelType("d", capacity_kn_per_m=3.0, min_length_mm=1800, lengths_mm=[1800, 2400], max_length_mm=2700),
    PanelType("h(B)", capacity_kn_per_m=5.2, min_length_mm=900, lengths_mm=[900, 1200, 1800, 2400, 3600]),
    PanelType("g", capacity_kn_per_m=3.4, min_length_mm=600, lengths_mm=[600]),
]
RULES = BracingRules(max_spacing_mm=9000, corner_zone_mm=1200)


def _storey():
    ext = _rect_walls(W=14000, H=11000, thk=90)
    ext[0].openings = [Opening(id="a", offset_mm=3000, width_mm=1800), Opening(id="b", offset_mm=9000, width_mm=2400)]
    internal = [Wall(id="I1", start=Pt(x=7000, y=0), end=Pt(x=7000, y=11000), thickness_mm=90, external=False, load_bearing=False),
                Wall(id="I2", start=Pt(x=0, y=5500), end=Pt(x=14000, y=5500), thickness_mm=90, external=False, load_bearing=False)]
    return Storey(id="g", walls=ext + internal)


def test_bracing_meets_demand_with_corner_panels_and_spacing():
    s = _storey()
    res = place_bracing(s, "x", demand_kn=30.0, types=TYPES, rules=RULES, footprint_extent=(0, 11000))
    assert res.adequate, res.notes
    # both long external walls (y=0 and y=11000) get a corner-zone panel
    walls_used = {p.wall_id for p in res.panels}
    assert {"W0", "W2"} <= walls_used
    # every panel is a standard length and within its segment
    for p in res.panels:
        assert p.length_mm in p.type.lengths_mm
    # label format matches the practice: "d - 2400"
    assert all(" - " in p.label for p in res.panels)
    # x-direction panels lie on x-running walls
    assert all(abs(p.start.y - p.end.y) < 1 for p in res.panels)


def test_bracing_uses_internal_wall_when_spacing_exceeded():
    s = _storey()
    tight = BracingRules(max_spacing_mm=6000, corner_zone_mm=1200)
    res = place_bracing(s, "x", demand_kn=5.0, types=TYPES, rules=tight, footprint_extent=(0, 11000))
    assert "I2" in {p.wall_id for p in res.panels}, res.notes


def test_bracing_reports_shortfall():
    s = _storey()
    res = place_bracing(s, "y", demand_kn=500.0, types=TYPES, rules=RULES, footprint_extent=(0, 14000))
    assert not res.adequate and any("add bracing" in n for n in res.notes)


def test_lintel_tags_ascending_by_size():
    inst = [LintelInstance("W0", Opening(id="a", offset_mm=0, width_mm=1800), "190x35 F17/LVL"),
            LintelInstance("W0", Opening(id="b", offset_mm=0, width_mm=900), "90x35 F17/LVL"),
            LintelInstance("W1", Opening(id="c", offset_mm=0, width_mm=2400), "190x35 F17/LVL"),
            LintelInstance("W1", Opening(id="d", offset_mm=0, width_mm=3300), "250 PFC")]
    tags = assign_lintel_tags(inst, ["90x35", "120x35", "140x35", "190x35", "190x45", "240x45"])
    assert tags == {"90x35 F17/LVL": "L1", "190x35 F17/LVL": "L2", "250 PFC": "L3"}
    assert [i.tag for i in inst] == ["L2", "L1", "L2", "L3"]
