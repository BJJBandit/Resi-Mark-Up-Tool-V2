from app.design import plan
from app.model import Building, Deck, Floor, Opening, Polygon, Project, Pt, Roof, Storey, Wall


def _wall(i, ext=True, openings=0, construction="timber_stud"):
    return Wall(id=f"W{i}", start=Pt(x=0, y=i * 1000), end=Pt(x=6000, y=i * 1000), thickness_mm=90, external=ext,
                construction=construction, load_bearing=True if ext else None,
                openings=[Opening(id=f"O{i}{k}", offset_mm=500 * k, width_mm=1200) for k in range(openings)])


def test_single_storey_brick_veneer_truss_roof():
    b = Building(id="u1", external_wall_construction="brick_veneer",
                 roof=Roof(form="hip", construction="truss", pitch_deg=22.5, cladding="sheet"),
                 storeys=[Storey(id="g", name="ground", floor=Floor(kind="suspended_timber"),
                                 walls=[_wall(0, openings=2), _wall(1, ext=False)])])
    p = plan(Project(buildings=[b]))
    ids = [s.id for s in p.active()]
    assert ids.index("wind") < ids.index("rack_u1_g") < ids.index("tie_u1_g")
    assert ids.index("site") < ids.index("foot_u1") < ids.index("pier_u1")
    assert "floor_u1_g" in ids and "artic_u1_g" in ids and "lint_u1_g" in ids
    skipped = {s.id: s.skipped for s in p.steps if s.skipped}
    assert "roof_u1" in skipped and "trusses" in skipped["roof_u1"]
    assert "deck_u1" in skipped
    assert p.sheets["u1"] == ["footing_floor", "wall", "roof"]
    assert "load_path" in p.background
    assert "flowchart TD" in p.mermaid() and "skipped" in p.mermaid()


def test_two_storey_orders_upper_before_lower_and_sheets():
    b = Building(id="h", external_wall_construction="timber_stud",
                 roof=Roof(construction="truss"),
                 decks=[Deck(id="d1", outline=Polygon(points=[Pt(x=0, y=0), Pt(x=1, y=0), Pt(x=1, y=1)]))],
                 storeys=[Storey(id="gf", name="ground", floor=Floor(kind="suspended_timber"), walls=[_wall(0)]),
                          Storey(id="ff", name="first", floor=Floor(kind="suspended_timber"), walls=[_wall(1)])])
    p = plan(Project(buildings=[b]))
    ids = [s.id for s in p.active()]
    assert ids.index("rack_h_ff") < ids.index("rack_h_gf")
    lower = next(s for s in p.steps if s.id == "rack_h_gf")
    assert lower.params["storey_position"] == "lower" and "rack_h_ff" in lower.needs
    assert "deck_h" in ids
    assert not any(s.id.startswith("artic") and not s.skipped for s in p.steps)
    assert p.sheets["h"][0] == "footing_slab" and len(p.sheets["h"]) == 5


def test_class_10a_garage_gets_outbuilding_sheets():
    g = Building(id="garage", ncc_class="10a", external_wall_construction="timber_stud",
                 storeys=[Storey(id="g", floor=Floor(kind="slab_on_ground"), walls=[_wall(0)])])
    p = plan(Project(buildings=[g]))
    assert p.sheets["garage"] == ["footing_slab", "roof_wall"]
