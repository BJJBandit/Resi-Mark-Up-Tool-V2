"""Tests for the deterministic design-engine framework: table loader, logic interpreter,
wind and footing engines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.engines import ENGINES, footings, wind
from app.engines.logic import LogicError, load_all_trees, tree_from_dict, tree_to_mermaid, walk
from app.engines.prefs import conservative_merge, load_preferences
from app.engines.tables import OutOfRange, Table, TableNotLoaded, TableSet, default_tables
from app.engines.trace import Trace
from app.model.schema import Building, Floor, Geotech, Project, Pt, Site, Storey, Wall, WindInputs

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _table(tmp_path: Path, name: str, meta: dict, rows: list[dict]) -> Table:
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump({**meta, "rows": rows}))
    return Table.from_yaml(p, table_id=name)


def _square(size: float = 10000) -> list[Wall]:
    pts = [Pt(x=0, y=0), Pt(x=size, y=0), Pt(x=size, y=size), Pt(x=0, y=size)]
    return [
        Wall(id=f"W{i}", start=pts[i], end=pts[(i + 1) % 4], thickness_mm=90, external=True, load_bearing=True)
        for i in range(4)
    ]


def _project(site_class="M", floor_kind="suspended_timber", wall="brick_veneer", wind_kw=None, geotech_kw=None) -> Project:
    geotech = Geotech(report_provided=True, site_class=site_class, **(geotech_kw or {}))
    return Project(
        project_name="Test",
        site=Site(geotech=geotech, wind=WindInputs(**(wind_kw or {}))),
        buildings=[
            Building(
                id="B1",
                external_wall_construction=wall,
                storeys=[Storey(id="G", name="ground", walls=_square(), floor=Floor(kind=floor_kind))],
            )
        ],
    )


@pytest.fixture
def prefs() -> dict:
    return load_preferences()


@pytest.fixture
def repo_tables() -> TableSet:
    return default_tables(reload=True)


# ---------------------------------------------------------------------------
# 1. table loader
# ---------------------------------------------------------------------------


def test_empty_table_raises_not_loaded_with_citation(repo_tables):
    t = repo_tables.get("as4055_2021/table_2_2_region_A_wind_class")
    assert not t.loaded
    with pytest.raises(TableNotLoaded) as ei:
        t.lookup(terrain_category="TC3", shielding="PS", topographic_class="T0")
    assert ei.value.citation == "AS 4055:2021 Table 2.2"
    assert "not loaded" in str(ei.value)


def test_repo_templates_all_parse_and_are_empty(repo_tables):
    status = repo_tables.status()
    assert len(status) >= 9
    assert all(not s["loaded"] for s in status), "no table may ship with values"
    for s in status:
        assert s["citation"].startswith("AS ")


def test_ceil_rule_picks_next_bound_up(tmp_path):
    t = _table(
        tmp_path,
        "spans",
        {"standard": "TEST", "edition": "1", "table": "1", "inputs": ["grade", "span_mm"], "outputs": ["size"],
         "numeric_rule": {"span_mm": "ceil"}},
        [
            {"grade": "F7", "span_mm": 2400, "size": "140x45"},
            {"grade": "F7", "span_mm": 3000, "size": "190x45"},
            {"grade": "F7", "span_mm": 3600, "size": "240x45"},
            {"grade": "F17", "span_mm": 3000, "size": "140x45"},
        ],
    )
    assert t.lookup(grade="F7", span_mm=2900) == {"size": "190x45"}  # rounded up to 3000
    assert t.lookup(grade="F7", span_mm=3000) == {"size": "190x45"}  # exact bound
    assert t.lookup(grade="F17", span_mm=100) == {"size": "140x45"}
    with pytest.raises(OutOfRange):
        t.lookup(grade="F7", span_mm=4000)  # beyond the table
    with pytest.raises(OutOfRange):
        t.lookup(grade="MGP12", span_mm=2000)  # categorical mismatch


def test_floor_rule_and_dash_cells(tmp_path):
    t = _table(
        tmp_path, "bearing",
        {"standard": "TEST", "edition": "1", "table": "2", "inputs": ["kpa"], "outputs": ["width_mm"],
         "numeric_rule": {"kpa": "floor"}},
        [{"kpa": 100, "width_mm": 600}, {"kpa": 150, "width_mm": 450}, {"kpa": 200, "width_mm": None}],
    )
    assert t.lookup(kpa=170) == {"width_mm": 450}  # read down to 150
    with pytest.raises(OutOfRange):
        t.lookup(kpa=90)
    with pytest.raises(OutOfRange):  # dash in the standard = not permitted
        t.lookup(kpa=200)


def test_interpolation_only_when_declared(tmp_path):
    rows = [{"x": 0, "y": 0}, {"x": 10, "y": 100}]
    meta = {"standard": "TEST", "edition": "1", "table": "3", "inputs": ["x"], "outputs": ["y"]}
    assert _table(tmp_path, "a", meta, rows).lookup(x=5) == {"y": 100}  # ceil, no interpolation
    assert _table(tmp_path, "b", {**meta, "interpolate": True}, rows).lookup(x=5) == {"y": 50.0}


def test_csv_sidecar_rows(tmp_path):
    (tmp_path / "csvt.yaml").write_text(yaml.safe_dump(
        {"standard": "TEST", "edition": "1", "table": "4", "inputs": ["roof_type", "rlw"], "outputs": ["span_single"], "rows": []}))
    (tmp_path / "csvt.csv").write_text("roof_type,rlw,span_single\nsheet,1500,3100\nsheet,3000,2400\ntile,1500,\n")
    t = Table.from_yaml(tmp_path / "csvt.yaml", table_id="csvt")
    assert t.loaded and t.lookup(roof_type="sheet", rlw=2000) == {"span_single": 2400}
    with pytest.raises(OutOfRange):
        t.lookup(roof_type="tile", rlw=1500)


def test_tables_status_report(repo_tables):
    row = next(s for s in repo_tables.status() if s["id"] == "as2870_2011/table_3_4_strip_footings")
    assert row["loaded"] is False and row["rows"] == 0 and row["source_checked_by"] == ""
    assert row["citation"] == "AS 2870:2011 Table 3.4"


# ---------------------------------------------------------------------------
# 2. logic interpreter
# ---------------------------------------------------------------------------

TINY = {
    "engine": "tiny",
    "title": "Tiny tree",
    "inputs": ["x", "cls"],
    "start": "q",
    "nodes": [
        {"id": "q", "ask": "x", "branches": [
            {"when": {"lt": 10}, "goto": "small"},
            {"when": {"ge": 10}, "goto": "big"},
            {"else": True, "goto": "none"},
        ]},
        {"id": "small", "result": {"size": "S"}, "goto": "look"},
        {"id": "big", "result": {"size": "L"}, "flag": "big one", "end": True},
        {"id": "none", "end": True, "status": "blocked", "rule": "x missing", "citation": "TEST cl 1"},
        {"id": "look", "lookup": "t/x", "with": {"cls": "cls"}, "into": "row", "goto": "fin"},
        {"id": "fin", "end": True},
    ],
}


def _tiny_tables(loaded: bool) -> TableSet:
    rows = [{"cls": "A", "v": 1}] if loaded else []
    return TableSet([Table(id="t/x", standard="TEST", edition="1", table="X", title="x", inputs=["cls"], outputs=["v"], rows=rows)])


def test_walk_takes_right_branch_and_records_nodes():
    tree = tree_from_dict(TINY)
    trace = Trace()
    with trace.section("tiny", "run"):
        w = walk(tree, {"x": 3, "cls": "A"}, _tiny_tables(True), trace)
    assert w.visited == ["q", "small", "look", "fin"]
    assert w.results["size"] == "S" and w.results["row"] == {"v": 1} and w.vars["v"] == 1
    assert w.status == "ok"
    recorded = [s.node_id for s in trace.steps[0].children]
    assert recorded == ["q", "small", "look", "fin"]
    assert trace.steps[0].children[0].kind == "decision"
    assert trace.steps[0].children[2].kind == "lookup"


def test_walk_flag_blocked_and_unloaded_lookup():
    tree = tree_from_dict(TINY)
    w = walk(tree, {"x": 12}, _tiny_tables(True), None)
    assert w.visited == ["q", "big"] and w.status == "flag" and w.flags == ["big one"]

    w = walk(tree, {"cls": "A"}, _tiny_tables(True), None)
    assert w.visited == ["q", "none"] and w.status == "blocked" and w.blocked_on == ["TEST cl 1"]

    w = walk(tree, {"x": 1, "cls": "A"}, _tiny_tables(False), None)
    assert w.status == "blocked" and w.blocked_on == ["TEST:1 Table X"]


def test_mermaid_contains_every_node_and_marks_taken_path():
    tree = tree_from_dict(TINY)
    mmd = tree_to_mermaid(tree)
    for n in tree.nodes:
        assert f"\n  {n}" in mmd
    assert "-->" in mmd and "-.->" not in mmd
    w = walk(tree, {"x": 3, "cls": "A"}, _tiny_tables(True), None)
    mmd = tree_to_mermaid(tree, w)
    assert "q ==>" in mmd  # taken edge thick
    assert "-.->" in mmd  # untaken dotted
    assert "class big nottaken" in mmd and "class small ok" in mmd


def test_tree_validation_catches_bad_goto():
    bad = {**TINY, "nodes": [{"id": "q", "ask": "x", "branches": [{"else": True, "goto": "nowhere"}]}]}
    with pytest.raises(LogicError):
        tree_from_dict(bad)


def test_repo_trees_load_and_registry_matches():
    trees = load_all_trees()
    assert set(trees) == set(ENGINES) == {"wind_as4055", "footings_as2870"}
    for t in trees.values():
        assert t.tables, "each tree lists the tables it depends on"


# ---------------------------------------------------------------------------
# 3. wind engine
# ---------------------------------------------------------------------------


def test_wind_stated_class_without_inputs_is_flagged(repo_tables, prefs):
    p = _project(wind_kw={"stated_class": "N2", "stated_source": "architect"})
    trace = Trace()
    r = wind.run(p, "B1", repo_tables, prefs, trace)
    assert r.wind_class == "N2" and r.source == "stated"
    assert "use_stated" in r.visited
    flagged = [s for s in trace.all_steps() if s.node_id == "use_stated"]
    assert flagged and flagged[0].status == "flag" and "not verified" in flagged[0].rule
    assert "AS 4055:2021 cl 1.2" in r.blocked_on  # scope limits still TODO
    json.loads(trace.to_json())  # serialisable


def test_wind_full_inputs_blocked_on_table_2_2(repo_tables, prefs):
    p = _project(wind_kw={"terrain_category": "TC3", "shielding": "PS", "topographic_class": "T0"})
    trace = Trace()
    r = wind.run(p, "B1", repo_tables, prefs, trace)
    assert r.status == "blocked"
    assert "AS 4055:2021 Table 2.2" in r.blocked_on
    lookup = [s for s in trace.all_steps() if s.node_id == "class_lookup_a"][0]
    assert lookup.status == "blocked" and lookup.citation == "AS 4055:2021 Table 2.2"
    assert r.wind_class is None
    assert "Table 2.2" in trace.to_markdown()


def test_wind_loaded_table_derives_and_flags_disagreement(repo_tables, prefs):
    tables = TableSet(list(repo_tables))
    t22 = repo_tables.get("as4055_2021/table_2_2_region_A_wind_class")
    tables.add(Table(**{**t22.__dict__, "rows": [
        {"terrain_category": "TC3", "shielding": "PS", "topographic_class": "T0", "wind_class": "N2"}]}))
    p = _project(wind_kw={"terrain_category": "TC3", "shielding": "PS", "topographic_class": "T0", "stated_class": "N3"})
    trace = Trace()
    r = wind.run(p, "B1", tables, prefs, trace)
    assert r.derived_class == "N2" and "disagrees" in r.visited
    # stated N3 is more severe than derived N2 -> adopted conservatively, with a note
    assert r.wind_class == "N3" and r.source == "stated_conservative"
    assert any(s.conservatism_note for s in trace.all_steps())
    p2 = _project(wind_kw={"terrain_category": "TC3", "shielding": "PS", "topographic_class": "T0", "stated_class": "N2"})
    r2 = wind.run(p2, "B1", tables, prefs, Trace())
    assert r2.wind_class == "N2" and "agrees" in r2.visited


def test_wind_region_c_not_supported(repo_tables, prefs):
    p = _project(wind_kw={"region": "C", "terrain_category": "TC2", "shielding": "NS", "topographic_class": "T0"})
    r = wind.run(p, "B1", repo_tables, prefs, Trace())
    assert "class_lookup_c" in r.visited
    assert r.status in ("not_supported", "blocked")  # scope TODO may still block
    assert "AS 4055:2021 Table 2.4" not in r.blocked_on  # not_supported, not blocked


# ---------------------------------------------------------------------------
# 4. footings engine
# ---------------------------------------------------------------------------


def test_footings_class_p_engineered_with_piers(repo_tables, prefs):
    p = _project(site_class="P", floor_kind="suspended_timber", wall="timber_stud")
    trace = Trace()
    r = footings.run(p, "B1", repo_tables, prefs, trace)
    assert r.footing_system == "piers_to_rock" and r.design_basis == "engineered"
    assert "class_p" in r.visited and "class_p_strip" in r.visited
    assert any("engineering design" in f for f in r.flags)
    assert r.proposal["piers"]["diameter_mm"] == 450 and r.proposal["depth_mm"] == 500  # class P depth
    assert r.construction_type == "clad_frame"
    paths = [(s.load_from, s.load_to) for s in trace.load_paths()]
    assert ("strip footing SF1", "bored piers P2") in paths and ("bored piers P2", "rock / refusal") in paths


def test_footings_class_m_brick_veneer_suspended_blocked_on_table_3_4(repo_tables, prefs):
    p = _project(site_class="M", floor_kind="suspended_timber", wall="brick_veneer")
    trace = Trace()
    r = footings.run(p, "B1", repo_tables, prefs, trace)
    assert r.footing_system == "strip_and_piers" and r.construction_type == "articulated_masonry_veneer"
    assert r.visited[-3:] == ["strip_and_piers", "strip_lookup", "done"]
    assert "AS 2870:2011 Table 3.4" in r.blocked_on and r.status == "blocked" and not r.verified
    lookup = [s for s in trace.all_steps() if s.node_id == "strip_lookup"][0]
    assert lookup.status == "blocked" and lookup.citation == "AS 2870:2011 Table 3.4"
    # practice proposal still carried and marked as a preference
    proposal_step = [s for s in trace.all_steps() if s.preference_applied and s.result.get("tag") == "SF1"][0]
    assert proposal_step.status == "flag" and "not yet loaded" in proposal_step.rule
    assert r.proposal["width_mm"] == 450 and r.proposal["depth_mm"] == 450 and r.proposal["piers"]["max_spacing_mm"] == 3000
    # load path roof -> walls -> strip -> piers
    chain = [(s.load_from, s.load_to) for s in trace.load_paths()]
    assert chain[0][0] == "roof" and ("ground-storey external walls", "strip footing SF1") in chain
    md = trace.to_markdown()
    assert "BLOCKED" in md and "Table 3.4" in md
    assert "Load path" in trace.to_mermaid()


def test_footings_table_wins_when_deeper_than_preference(repo_tables, prefs):
    tables = TableSet(list(repo_tables))
    t34 = repo_tables.get("as2870_2011/table_3_4_strip_footings")
    tables.add(Table(**{**t34.__dict__, "rows": [
        {"site_class": "M", "construction_type": "articulated_masonry_veneer", "depth_mm": 600, "width_mm": 300, "reinforcement": "3-L8TM"}]}))
    p = _project(site_class="M", floor_kind="suspended_timber", wall="brick_veneer")
    trace = Trace()
    r = footings.run(p, "B1", tables, prefs, trace)
    assert r.verified and r.status != "blocked"
    assert r.proposal["depth_mm"] == 600  # table deeper than practice 450 -> table wins
    assert r.proposal["width_mm"] == 450  # practice wider than table 300 -> practice kept
    assert r.conservatism_notes and "depth_mm" in r.conservatism_notes[0]
    step = [s for s in trace.all_steps() if s.conservatism_note][0]
    assert "Table 3.4" in step.conservatism_note and step.preference_applied


def test_footings_slab_on_ground_goes_to_raft_table_3_1(repo_tables, prefs):
    p = _project(site_class="S", floor_kind="slab_on_ground", wall="brick_veneer")
    r = footings.run(p, "B1", repo_tables, prefs, Trace())
    assert r.footing_system == "stiffened_raft" and "raft_lookup" in r.visited
    assert "AS 2870:2011 Table 3.1" in r.blocked_on
    assert r.proposal["edge_beam_depth_mm"] == 450 and r.proposal["slab_thickness_mm"] == 100


def test_footings_strip_on_rock_omits_piers(repo_tables, prefs):
    p = _project(site_class="A", floor_kind="suspended_timber", wall="timber_stud", geotech_kw={"rock_depth_mm": 300})
    trace = Trace()
    r = footings.run(p, "B1", repo_tables, prefs, trace)
    assert r.footing_system == "strip_on_rock" and "piers" not in r.proposal
    assert ("strip footing SF1", "rock") in [(s.load_from, s.load_to) for s in trace.load_paths()]


def test_footings_unknown_site_class_blocked(repo_tables, prefs):
    r = footings.run(_project(site_class="Unknown"), "B1", repo_tables, prefs, Trace())
    assert r.status == "blocked" and r.footing_system is None and "site_class_unknown" in r.visited


def test_conservative_merge_notes_string_differences():
    merged, notes = conservative_merge({"depth_mm": 450, "reinforcement": "4/L11"}, {"depth_mm": 400, "reinforcement": "3-L8"}, ("depth_mm",), citation="T")
    assert merged["depth_mm"] == 450 and notes and "reinforcement" in notes[0]


# ---------------------------------------------------------------------------
# 5. routes
# ---------------------------------------------------------------------------


def test_routes_render():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from app.routes_logic import router

    app = fastapi.FastAPI()
    app.include_router(router)
    c = TestClient(app)
    page = c.get("/design-logic")
    assert page.status_code == 200 and "mermaid" in page.text and "footings_as2870" in page.text and "not loaded" in page.text
    mmd = c.get("/design-logic/wind_as4055.mmd")
    assert mmd.status_code == 200 and mmd.text.startswith("flowchart TD") and "class_lookup_a" in mmd.text
    assert c.get("/design-logic/nope.mmd").status_code == 404
    st = c.get("/tables/status").json()
    assert st["total"] >= 9 and st["loaded"] == 0
