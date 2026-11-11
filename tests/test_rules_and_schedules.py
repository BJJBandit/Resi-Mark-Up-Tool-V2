import pymupdf
import pytest

from app import office_checks, stock
from app.pdf import _fit_region, _sizes, _turn_span_arrows, format_ranges, parse_page_range
from app.pipeline import JobInput, _wanted, short_spec
from app.rules import apply_rules
from app.schemas import Box, Element, MarkupSheet, Point, ScheduleItem
from app.schemas import SheetKind as K
from app.usage import Call, cache_checks


def sheet(elements=(), schedule=()):
    return MarkupSheet(source_page_number=1, plan_region=Box(x1=0, y1=0, x2=1, y2=1), classification="",
                       schedule_title="", schedule=list(schedule), legend_title="", legend=[],
                       elements=list(elements), notes=[])


def el(kind, tag, x1, y1, x2, y2, text=""):
    return Element(kind=kind, tag=tag, start=Point(x=x1, y=y1), end=Point(x=x2, y=y2), text=text)


# --- firm rules -------------------------------------------------------------------

def test_strip_footing_and_beam_widths_are_enforced():
    s = sheet(schedule=[ScheduleItem(tag="SF1", text="300W x 450 DEEP STRIP FOOTING"),
                        ScheduleItem(tag="EB1", text="300W x 450 DEEP EDGE BEAM"),
                        ScheduleItem(tag="P1", text="450 DIA BORED PIER")])
    warnings = apply_rules(s, "FOOTING PLAN")
    assert "450W" in s.schedule[0].text and "450W" in s.schedule[1].text
    assert s.schedule[2].text == "450 DIA BORED PIER"
    assert len(warnings) == 2


# --- schedules: type and spacing only ----------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("90X35 F17/LVL LINTEL - A2106 (600 SPAN) IN RECESS RETURN WALL, NO ROOF LOAD", "90X35 F17/LVL LINTEL"),
    ("140X45 F7 TP DECK JOIST AT 400 CTRS (EAST DECK, MAX SPAN 2460)", "140X45 F7 TP DECK JOIST AT 400 CTRS"),
    ("2/190X45 F7 TP DECK BEARER (EAST DECK) ON POSTS AT MAX 2000 CTRS", "2/190X45 F7 TP DECK BEARER AT MAX 2000 CTRS"),
    ("450W X 450 DEEP STRIP FOOTING (MIN), N25 CONCRETE, 4/L11TM TOP AND BOTTOM, 50 COVER. SPANS BETWEEN P1",
     "450W X 450 DEEP STRIP FOOTING, 4/L11TM TOP AND BOTTOM"),
    ("TYPE (h) STRUCTURAL PLYWOOD BRACING PANEL (MIN 900 LONG) TO AS 1684.2", "TYPE (h) STRUCTURAL PLYWOOD BRACING PANEL"),
])
def test_short_spec(raw, want):
    assert short_spec(raw) == want


# --- office stock list ---------------------------------------------------------------

def test_stock_sizes_pass_and_others_are_named():
    ok = [ScheduleItem(tag="L1", text="90X35 F17/LVL LINTEL"), ScheduleItem(tag="B1", text="2/240X45 F7 TP BEARER"),
          ScheduleItem(tag="C1", text="89X3.5 SHS COLUMN"), ScheduleItem(tag="BM", text="250UB31.4 BEAM"),
          ScheduleItem(tag="SF1", text="450W X 450 DEEP STRIP FOOTING, 4/L11TM TOP AND BOTTOM"),
          ScheduleItem(tag="C2", text="89X5 SHS POST ON 90X190X10 PLATE")]
    assert stock.check(ok, "T") == []
    bad = [ScheduleItem(tag="VB1", text="290X45 F17/LVL VERANDAH BEAM"), ScheduleItem(tag="X", text="2/240X45 MGP12 LINTEL"),
           ScheduleItem(tag="BM2", text="200UB25.4 BEAM")]
    (warning,) = stock.check(bad, "T")
    assert all(t in warning for t in ("VB1", "X", "BM2"))


# --- which sheets get made -------------------------------------------------------------

def job(sheets, combine=False):
    return JobInput(project="", arch_pdf=b"", geotech_pdf=None, wind_class=None, site_class=None,
                    construction="", notes="", sheets=sheets, combine_foundation=combine)


def test_wanted_sheets():
    assert _wanted(job([K.WALLS, K.FOUNDATION])) == [K.WALLS, K.FOUNDATION]
    assert _wanted(job([K.FOOTINGS, K.FLOOR])) == [K.FOOTINGS, K.FLOOR]
    assert _wanted(job([K.FOUNDATION, K.FOOTINGS])) == [K.FOUNDATION]  # combined covers both
    assert K.FOUNDATION in _wanted(job([], combine=True)) and K.FOOTINGS not in _wanted(job([], combine=True))


# --- page ranges -------------------------------------------------------------------------

def test_page_ranges():
    assert parse_page_range("3-5, 8", 10) == [3, 4, 5, 8]
    assert parse_page_range("", 3) == [1, 2, 3]
    assert format_ranges([1, 2, 3, 7]) == "1-3, 7"
    with pytest.raises(ValueError):
        parse_page_range("9-12", 10)


# --- drawing helpers -----------------------------------------------------------------------

def test_sizes_read_from_schedule_text():
    assert _sizes("230X230 BRICK PIER ON 450 DIA BORED PIER") == (450.0, 230.0)
    assert _sizes("89X3.5 SHS COLUMN") == (None, 89.0)


def test_plan_region_grows_to_take_in_marks_outside_it():
    s = sheet([el("member", "EB1", .2, .2, .4, .2), el("member", "EB1", .6, .6, .8, .6)])
    s.plan_region = Box(x1=.5, y1=.5, x2=.9, y2=.9)
    assert _fit_region(s) == 1
    assert s.plan_region.x1 < .2 and s.plan_region.y1 < .2


def _px(e):
    return pymupdf.Point(e.start.x * 100, e.start.y * 100), pymupdf.Point(e.end.x * 100, e.end.y * 100)


def test_every_joist_gets_its_own_arrow_across_it():
    s = sheet([el("member", "J2", .5, .1, .5, .2),      # north deck joist (up / down the page)
               el("span", "J2", .3, .15, .7, .15),      # its arrow: already across
               el("member", "J2", .85, .5, .95, .5),    # east deck joist (across the page)
               el("span", "J2", .8, .45, .95, .45),     # its arrow drawn ALONG it: must turn
               el("member", "J1", .4, .3, .4, .8)])     # floor joist with no arrow: gets one

    def put(i, r):
        s.elements[i] = s.elements[i].model_copy(update={"start": Point(x=r[0].x / 100, y=r[0].y / 100),
                                                         "end": Point(x=r[1].x / 100, y=r[1].y / 100)})

    _turn_span_arrows(s, _px, put)
    arrows = [e for e in s.elements if e.kind == "span"]
    assert len(arrows) == 3

    def horizontal(e):
        return abs(e.end.x - e.start.x) >= abs(e.end.y - e.start.y)

    assert horizontal(arrows[0])        # north deck: across its vertical joist
    assert not horizontal(arrows[1])    # east deck: turned to run across its horizontal joist
    assert horizontal(arrows[2])        # J1: added across the vertical joist


# --- office practice checks ------------------------------------------------------------------

class _Page:  # A3 landscape in points
    rect = pymupdf.Rect(0, 0, 1190, 842)


def test_pier_spacing_over_office_limit_is_named():
    w = 1190
    mm = 72 / 25.4 / 100  # points per mm at 1:100
    xs = [200, 200 + 3000 * mm, 200 + (3000 + 4200) * mm]  # second gap is 4200 mm
    s = sheet([el("member", "B1", 200 / w, .5, xs[-1] / w, .5)] + [el("pier", "P2", x / w, .5, x / w, .5) for x in xs])
    (warning,) = office_checks.pier_spacing(s, _Page, 100, "FOOTING & FLOOR PLAN")
    assert "B1" in warning and "4200" in warning
    ok = sheet([el("member", "B1", 200 / w, .5, xs[1] / w, .5)] + [el("pier", "P2", x / w, .5, x / w, .5) for x in xs[:2]])
    assert office_checks.pier_spacing(ok, _Page, 100, "T") == []


def test_schedule_practice():
    s = sheet([el("bracing", "g - 600", .1 * i, .1, .1 * i + .05, .1) for i in range(1, 8)],
              [ScheduleItem(tag="B3", text="FLAME-SHIELD STEEL DECK BEARER"),
               ScheduleItem(tag="L1", text="2/190X45 MGP12 LINTEL"),
               ScheduleItem(tag="J2", text="140X45 F7 TP DECK JOIST AT 400 CTRS"),
               ScheduleItem(tag="J3", text="190X45 F7 TP DECK JOIST AT 400 CTRS")])
    text = " | ".join(office_checks.schedule_practice(s, "T"))
    assert "deck framing in steel (B3)" in text
    assert "not a single F17 / LVL member (L1)" in text
    assert "2 deck joist types" in text
    assert "all 7 bracing panels are type g" in text
    good = sheet(schedule=[ScheduleItem(tag="L3", text="140X45 F17/LVL LINTEL"),
                           ScheduleItem(tag="J2", text="190X45 F7 TP DECK JOIST AT 400 CTRS")])
    assert office_checks.schedule_practice(good, "T") == []


# --- cache checks -----------------------------------------------------------------------------

def test_cache_checks_report_shared_and_not_shared():
    def call(step, write=0, read=0):
        return Call(step=step, model="claude-opus-5-5", cache_write_1h=write, cache_read=read)

    shared = cache_checks([call("Step 2 (architectural review)", write=40000),
                           call("Step 3 (WALL FRAMING PLAN)", write=15000, read=40000),
                           call("Step 3 (FOOTING PLAN)", read=55000)])
    assert [c["ok"] for c in shared] == [True, True]
    broken = cache_checks([call("Step 2 (architectural review)", write=40000),
                           call("Step 3 (WALL FRAMING PLAN)", write=55000)])
    assert broken[0]["ok"] is False


def test_text_boxes_carry_their_font_size_for_editors():
    """Bluebeam / Acrobat edit from /DS and /RC; without them a clicked box jumps to 12 pt."""
    from app import pdf as P
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    a = P._text(page, pymupdf.Rect(100, 100, 250, 140), "C1 89X3.5 SHS\nCONTINUOUS", 6.5, P.RED, "Callout", align=1)
    obj = doc.xref_object(a.xref)
    assert "/DS (font: Helvetica,sans-serif 6.5pt; text-align:center; color:#FF0000)" in obj
    assert "/RC" in obj and "6.5pt" in obj.split("/RC", 1)[1]


def test_australian_spelling():
    from app.spelling import to_au
    assert to_au("BOLTS AT 600 CENTERS, GALVANIZED, 0.2 MM VAPOR BARRIER") == "BOLTS AT 600 CENTRES, GALVANISED, 0.2 MM VAPOUR BARRIER"
    assert to_au("Center of gray fiber") == "Centre of grey fibre"
    assert to_au("PARAMETERS, SIZE, CTRS") == "PARAMETERS, SIZE, CTRS"  # whole words only


def test_strip_footings_drawn_as_bands_with_closed_corners():
    """Solid outer line + dashed inner line, inside found from the outline (recess comes
    out right), corners closed."""
    from app import pdf as P
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    m = P.PlanMap(page, Box(x1=0, y1=0, x2=1, y2=1))
    m.mm_pt = 72 / 25.4 / 100 * m.scale

    def seg(x1, y1, x2, y2):
        return el("member", "SF1", x1 / 1190, y1 / 842, x2 / 1190, y2 / 842)

    # outline with a recess in the top edge
    s = sheet([seg(100, 100, 200, 100), seg(200, 100, 200, 150), seg(200, 150, 400, 150), seg(400, 150, 400, 100),
               seg(400, 100, 500, 100), seg(500, 100, 500, 400), seg(500, 400, 100, 400), seg(100, 400, 100, 100)])
    bands = P._footing_bands(s, m)
    assert all(len(v) == 2 for v in bands.values())
    recess_side = bands[id(s.elements[1])]  # vertical recess wall at x = 200: outside is to the right
    solid = next(l for l in recess_side if not l[2])
    dashed = next(l for l in recess_side if l[2])
    assert solid[0].x > dashed[0].x
    top_left = bands[id(s.elements[0])]
    left = bands[id(s.elements[7])]
    solid_top = next(l for l in top_left if not l[2])
    solid_left = next(l for l in left if not l[2])
    corner = min((solid_top[0], solid_top[1]), key=lambda p: p.x)
    assert min(abs(corner - solid_left[0]), abs(corner - solid_left[1])) < 0.5  # outer corner closes


def test_untagged_footing_lines_and_slab_cloud_follow_house_style():
    """Untagged runs: perimeter -> EB / SF tag, inside -> IB tag; a big slab cloud becomes S1 diagonal."""
    from app import pdf as P

    class Page:
        rect = pymupdf.Rect(0, 0, 1000, 1000)

    def seg(x1, y1, x2, y2, tag=""):
        return el("member", tag, x1 / 1000, y1 / 1000, x2 / 1000, y2 / 1000)

    s = sheet([seg(100, 100, 200, 100), seg(200, 100, 200, 150), seg(200, 150, 400, 150), seg(400, 150, 400, 100),
               seg(400, 100, 500, 100), seg(500, 100, 500, 400), seg(500, 400, 100, 400, "EB1"), seg(100, 400, 100, 100),
               seg(100, 250, 500, 250), seg(300, 150, 300, 400),
               el("cloud", "", .09, .09, .51, .41)],
              [ScheduleItem(tag="EB1", text="450W X 450 DEEP EDGE BEAM"), ScheduleItem(tag="IB1", text="450W X 450 DEEP INTERNAL BEAM"),
               ScheduleItem(tag="S1", text="100 THICK SLAB SL82 TOP")])
    s.plan_region = Box(x1=0, y1=0, x2=.6, y2=.5)
    warnings = P._footing_house_style(s, Page, "T")
    tags = [e.tag for e in s.elements if e.kind == "member"]
    assert tags[:8] == ["EB1"] * 8          # the whole outline, recess included
    assert tags[8:10] == ["IB1", "IB1"]     # the two internal runs
    assert not any(e.kind == "cloud" for e in s.elements)
    assert any(e.kind == "member" and e.tag == "S1" for e in s.elements)
    assert len(warnings) == 2


def test_footing_practice_checks():
    s = sheet(schedule=[ScheduleItem(tag="EB1", text="450W X MIN 450 DEEP EDGE BEAM WITH 3/L11TM BOTTOM"),
                        ScheduleItem(tag="SF1", text="450W X 450 DEEP STRIP FOOTING, 4/L11TM TOP AND BOTTOM"),
                        ScheduleItem(tag="S1", text="100 THICK SLAB, SL82 TOP, N20 CONCRETE")])
    text = " | ".join(office_checks.footing_practice(s, "T"))
    assert "(EB1)" in text and "SF1" not in text.split("|")[0]
    assert "S1 (N20)" in text


def test_tags_do_not_overlap_and_use_9pt():
    from app import pdf as P
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    m = P.PlanMap(page, Box(x1=0, y1=0, x2=1, y2=1))
    for i in range(12):  # a tight row of piers, all tagged P1
        c = pymupdf.Point(400 + i * 14, 400)
        m.boxes.append(pymupdf.Rect(c.x - 5, c.y - 5, c.x + 5, c.y + 5))
        m.label_requests.append(("point", (c, 5.0), "P1", P.TAG_FS, P.RED, "Pier Tag"))
    m.segments.append((pymupdf.Point(380, 400), pymupdf.Point(580, 400)))
    m.label_requests.append(("line", (pymupdf.Point(380, 400), pymupdf.Point(580, 400)), "EB1", P.TAG_FS, P.RED, "Member Tag"))
    P._place_labels(page, m)
    tags = [a for a in page.annots() if a.type[1] == "FreeText"]
    rects = [a.rect for a in tags]
    assert all(not r.intersects(q) for i, r in enumerate(rects) for q in rects[i + 1:])
    assert all("/Helv 9 Tf" in doc.xref_object(a.xref) or "/Helv 9.0 Tf" in doc.xref_object(a.xref) for a in tags)
    assert any(a.info["content"] == "EB1" for a in tags)


def test_short_spec_keeps_bracing_type_codes():
    assert short_spec("STRUCTURAL PLYWOOD BRACING PANEL TYPE (h) TO AS1684.2 TABLE 8.18 (6.0 KN/M)") == \
        "STRUCTURAL PLYWOOD BRACING PANEL TYPE (h) TO AS1684.2 TABLE 8.18"
    assert short_spec("DIAGONAL METAL STRAP BRACING TYPE (d) - TENSIONED") == "DIAGONAL METAL STRAP BRACING TYPE (d)"


def test_masonry_lintel_schedule_box_on_wall_sheet():
    from types import SimpleNamespace
    from app import pdf as P
    assert P._is_masonry(SimpleNamespace(external_wall_type="BRICK VENEER", framing_material="TIMBER", governing_standard=""))
    assert not P._is_masonry(SimpleNamespace(external_wall_type="WEATHERBOARD", framing_material="TIMBER", governing_standard="AS 1684"))
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    P._schedule(page, "LINTEL SCHEDULE:", [ScheduleItem(tag="L1", text="90x35 F17/LVL LINTEL")], "", [], masonry=True)
    text = " ".join(a.info["content"] for a in page.annots() if a.type[1] == "FreeText")
    assert "MASONRY LINTEL SCHEDULE:" in text and "100x75x8 UA" in text and "MIN 150" in text


def test_reentrant_bars_and_slab_callout():
    from app import pdf as P
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    m = P.PlanMap(page, Box(x1=0, y1=0, x2=1, y2=1))

    def seg(x1, y1, x2, y2, tag="EB1"):
        return Element(kind="member", tag=tag, start=Point(x=x1 / 1190, y=y1 / 842), end=Point(x=x2 / 1190, y=y2 / 842), text="")

    # outline with a notch in the top: two re-entrant corners at (200,150) and (400,150)
    els = [seg(100, 100, 200, 100), seg(200, 100, 200, 150), seg(200, 150, 400, 150), seg(400, 150, 400, 100),
           seg(400, 100, 500, 100), seg(500, 100, 500, 400), seg(500, 400, 100, 400), seg(100, 400, 100, 100),
           seg(100, 400, 500, 100, "S1")]
    s = sheet(els, [ScheduleItem(tag="S1", text="100 THICK N25 SLAB, SL82 MESH TOP, 30 COVER")])
    assert P._reentrant_bars(page, s, m) == 2
    assert any("RE-ENTRANT" in n for n in s.notes)
    m.specs = {x.tag: x.text for x in s.schedule}
    P._slab_line(page, els[-1], m(els[-1].start), m(els[-1].end), P.RED, m)
    ring = [a for a in page.annots() if a.type[1] == "Circle"]
    mid = (m(els[-1].start) + m(els[-1].end)) * 0.5
    assert ring and ring[0].rect.contains(mid)  # callout ring at the middle of the line
    texts = [a.info["content"] for a in page.annots() if a.type[1] == "FreeText"]
    assert "S1" in texts  # the ring holds the tag only - the schedule gives the detail
    assert ring[0].rect.width < 30


def test_reentrant_bars_ignore_porch_stub():
    """A porch edge beam butting into the outline part-way along an edge must not turn an
    outside corner into a re-entrant one (Schouten St markup 30)."""
    from app import pdf as P
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    m = P.PlanMap(page, Box(x1=0, y1=0, x2=1, y2=1))

    def seg(x1, y1, x2, y2, tag="EB1"):
        return Element(kind="member", tag=tag, start=Point(x=x1 / 1190, y=y1 / 842), end=Point(x=x2 / 1190, y=y2 / 842), text="")

    # L-shape: low wing 429-632 x 335-573, high wing 632-838 x 237-573; porch stub at x 616
    els = [seg(429, 573, 838, 573), seg(838, 573, 838, 237), seg(632, 237, 838, 237), seg(632, 237, 632, 335),
           seg(429, 335, 632, 335), seg(429, 335, 429, 573), seg(616, 244, 632, 244), seg(616, 244, 616, 335),
           seg(429, 573, 838, 237, "S1")]
    s = sheet(els, [ScheduleItem(tag="S1", text="100 THICK N25 SLAB")])
    assert P._reentrant_bars(page, s, m) == 1
    bars = [a for a in page.annots() if a.info["subject"] == "Re-entrant Bars"]
    corner = m(els[3].end)
    assert all(abs(pymupdf.Point(v) - corner) < 60 for a in bars for v in a.vertices)


def test_callouts_do_not_overlap_or_cross():
    """Callout boxes don't overlap each other and their leaders don't cross."""
    import random

    from app import pdf as P
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    m = P.PlanMap(page, Box(x1=0, y1=0, x2=1, y2=1))
    m.placed = pymupdf.Rect(380, 230, 900, 600)
    random.seed(3)
    cos = []
    for i in range(12):
        x, y = random.uniform(390, 890) / 1190, random.uniform(240, 590) / 842
        cos.append(Element(kind="callout", tag="", start=Point(x=x, y=y), end=Point(x=x, y=y),
                           text=f"NOTE {i} " + "SOME LONGER TEXT ABOUT THE FOOTINGS " * random.randint(1, 3)))
    unplaced = P._draw_callouts(page, cos, m)
    boxes = [a for a in page.annots() if a.info["subject"] == "Call-out"]
    assert len(boxes) + len(unplaced) == 12
    rects = m.boxes  # the text boxes (an annotation's rect also spans its leader)
    for i, r in enumerate(rects):
        for q in rects[i + 1:]:
            assert (r & q).is_empty or (r & q).get_area() < 1
    legs = [(pymupdf.Point(a.vertices[0]), pymupdf.Point(a.vertices[1])) for a in boxes]

    def side(a, b, c):
        return (c.x - a.x) * (b.y - a.y) - (c.y - a.y) * (b.x - a.x)

    for i, (p1, p2) in enumerate(legs):
        for p3, p4 in legs[i + 1:]:
            assert not (side(p3, p4, p1) * side(p3, p4, p2) < 0 and side(p1, p2, p3) * side(p1, p2, p4) < 0)


def test_external_slabs_separate():
    """Porch / paving slabs are separate from the house slab: the app adds the expansion
    joint note and warns when the porch is wrapped in edge beams."""
    from app import pdf as P

    def seg(x1, y1, x2, y2, tag="EB1", text=""):
        return Element(kind="member", tag=tag, start=Point(x=x1, y=y1), end=Point(x=x2, y=y2), text=text)

    house = [seg(0.2, 0.4, 0.8, 0.4), seg(0.8, 0.4, 0.8, 0.8), seg(0.8, 0.8, 0.2, 0.8), seg(0.2, 0.8, 0.2, 0.4)]
    porch = [seg(0.5, 0.3, 0.6, 0.3), seg(0.5, 0.3, 0.5, 0.4), seg(0.6, 0.3, 0.6, 0.4)]
    sched = [ScheduleItem(tag="S1", text="100 THICK N25 SLAB"), ScheduleItem(tag="S2", text="100 THICK PORCH SLAB, SL82")]
    s = sheet(house + porch + [seg(0.2, 0.8, 0.8, 0.4, "S1"), seg(0.5, 0.4, 0.6, 0.3, "S2")], sched)
    assert P._external_slab_note(s)
    assert any("ABELFLEX" in n for n in s.notes)
    assert not P._external_slab_note(s)  # once only
    assert office_checks.external_slabs(s, "F") and "S2" in office_checks.external_slabs(s, "F")[0]
    s2 = sheet(house + [seg(0.2, 0.8, 0.8, 0.4, "S1"), seg(0.5, 0.4, 0.6, 0.3, "S2")], sched)
    assert office_checks.external_slabs(s2, "F") == []  # separate porch: no warning
    s3 = sheet(house + [seg(0.2, 0.8, 0.8, 0.4, "S1")], sched[:1])
    assert not P._external_slab_note(s3)  # no external slab, no note


def test_window_beside_cavity_line_found():
    """A window in a wall with a cavity line drawn along it (broken only at the window)
    shows as an opening when its short glazing lines are kept (A1806, Schouten St)."""
    from app.snap import Geometry, _solid
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=400)
    sh = page.new_shape()
    for y in (100, 106):  # wall faces, 0-300 continuous except the window frame
        sh.draw_line((50, y), (350, y))
    sh.draw_line((50, 102.5), (180, 102.5))  # cavity line, broken at the window
    sh.draw_line((197, 102.5), (350, 102.5))
    for y in (101.8, 103.2, 104.4):  # short glazing lines in the window
        sh.draw_line((180, y), (197, y))
    for x in (180, 197):  # jambs
        sh.draw_line((x, 100), (x, 106))
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    geo = Geometry(page)
    old = _solid(geo, True, 103, 6, 50, 350)
    new = _solid(geo, True, 103, 6, 50, 350, glazing=True)
    gaps = [(new[i][1], new[i + 1][0]) for i in range(len(new) - 1)]
    assert len(old) == 1  # the window reads as solid wall without glazing
    assert any(178 <= a <= 182 and 195 <= b <= 199 for a, b in gaps)


def test_bracing_in_doorway_moves_onto_wall():
    """A bracing panel Claude draws right across a doorway (a cavity slider's opening, so
    no wall under it at all) slides onto the solid wall beside it (markup 1, 820 CSD)."""
    from app.snap import Geometry, snap_bracing
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=600)
    sh = page.new_shape()
    for x in (300, 302.3):  # internal wall faces, doorway between y 220 and 242
        sh.draw_line((x, 150), (x, 220))
        sh.draw_line((x, 242), (x, 330))
    for y in (220, 242):  # jambs
        sh.draw_line((300, y), (302.3, y))
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    geo = Geometry(page)
    r = snap_bracing(geo, pymupdf.Point(298, 220), pymupdf.Point(298, 242), 25.5)
    assert r is not None and "moved" in r[2]
    lo, hi = sorted((r[0].y, r[1].y))
    assert hi <= 220.5 or lo >= 241.5  # off the doorway
    assert abs((hi - lo) - 25.5) < 0.5


def test_missing_piers_warned():
    """Piers scheduled but hardly any drawn along a long perimeter footing (markup 2)."""
    doc = pymupdf.open()
    page = doc.new_page(width=1000, height=1000)

    def el(kind, tag, x1, y1, x2=None, y2=None):
        x2, y2 = (x1, y1) if x2 is None else (x2, y2)
        return Element(kind=kind, tag=tag, start=Point(x=x1 / 1000, y=y1 / 1000), end=Point(x=x2 / 1000, y=y2 / 1000), text="")

    eb = [el("member", "EB1", 100, 100, 400, 100), el("member", "EB1", 400, 100, 400, 400),
          el("member", "EB1", 400, 400, 100, 400), el("member", "EB1", 100, 400, 100, 100)]
    sched = [ScheduleItem(tag="P1", text="450 DIA BORED PIER"), ScheduleItem(tag="EB1", text="450 X 450 EDGE BEAM")]
    few = sheet(eb + [el("pier", "P1", 100, 100)], sched)
    assert any("only 1 pier" in w for w in office_checks.pier_spacing(few, page, 100, "F"))
    piers = [el("pier", "P1", x, y) for x in range(100, 401, 75) for y in (100, 400)] + \
        [el("pier", "P1", x, y) for x in (100, 400) for y in range(175, 400, 75)]
    many = sheet(eb + piers, sched)
    assert not any("only" in w for w in office_checks.pier_spacing(many, page, 100, "F"))


def _calc_design(force_up=10.0, force_across=10.0):
    from app.schemas import BracingCapacity, DesignAssessment, RackingCase

    def case(walls, f):
        return RackingCase(wind_direction=f"Wind {walls}", bracing_walls=walls, storey="single", width_m=8,
                           roof_pitch_deg=22.5, area_working="x", area_m2=f / 0.5, pressure_kpa=0.5,
                           pressure_ref="T8.2", force_kn=f)
    return DesignAssessment.model_construct(
        racking=[case("up the page", force_up), case("across the page", force_across)],
        bracing_capacities=[BracingCapacity(type="h", description="ply", kn_per_m=6.0, ref="T8.18"),
                            BracingCapacity(type="d", description="strap", kn_per_m=1.5, ref="T8.18")],
        calculations=[], racking_summary="", wind_classification_used="N2", storeys=1, external_wall_type="BV",
        framing_material="timber", roof_form="hip", roof_construction="trusses", roof_pitch="22.5",
        roof_cladding="sheet", wall_height="2400", governing_standard="AS 1684.2")


def test_bracing_check_totals_each_direction():
    """Panels on the wall plan are totalled per direction (h(b) matched to the 'h' capacity)."""
    from app import calcs

    def br(tag, x1, y1, x2, y2):
        return Element(kind="bracing", tag=tag, start=Point(x=x1, y=y1), end=Point(x=x2, y=y2), text="")

    walls = MarkupSheet.model_construct(source_page_number=1, elements=[
        br("h(b) - 900", 0.2, 0.2, 0.2, 0.25), br("h(b) - 1200", 0.8, 0.2, 0.8, 0.27),  # up the page
        br("d - 2400", 0.3, 0.5, 0.4, 0.5),  # across the page
        br("x - 900", 0.3, 0.6, 0.35, 0.6)])  # type with no capacity
    chk = calcs.bracing_check(_calc_design(force_up=12.0, force_across=10.0), walls, 1.414)
    up, across = chk
    assert abs(up.provided_kn - 2.1 * 6.0) < 1e-6 and up.ok
    assert abs(across.provided_kn - 2.4 * 1.5) < 1e-6 and not across.ok and across.missing == ["x"]
    w = calcs.warnings(chk)
    assert any("less than the racking force 10.0 kN" in x for x in w) and any("type(s) x" in x for x in w)
    text = "\n".join(calcs.lines(GeoStub(), _calc_design(), chk, []))
    assert "HOW THE TIMBER BRACING IS DETERMINED" in text and "NOT ADEQUATE - ADD BRACING" in text


class GeoStub:
    site_classification = "M"
    ys = "YS 40 MM"
    wind_classification = "N2"


def test_calc_sheet_page_added():
    from app import calcs
    from app import pdf as P
    out = pymupdf.open()
    meta = P.SheetMeta("JOB", "", "A", "", "01/01/2026")
    P._calc_sheet(out, meta, calcs.lines(GeoStub(), _calc_design(), [], ["AS 1684.2"]))
    assert out.page_count == 1
    texts = " ".join(a.info["content"] for a in out[0].annots())
    assert "CALCULATION SHEET" in texts and "RACKING FORCES" in texts


def test_calc_sheet_has_no_title_block():
    from app import calcs
    from app import pdf as P
    out = pymupdf.open()
    meta = P.SheetMeta("JOB", "J1", "A", "DM", "01/01/2026", title_block=True)
    P._calc_sheet(out, meta, calcs.lines(GeoStub(), _calc_design(), [], []))
    texts = " ".join(a.info["content"] for a in out[0].annots())
    assert "SSK-C1" not in texts and "Job No" not in texts


def test_short_spec_keeps_abbreviations():
    assert short_spec("DIAGONAL 30x0.8 G.I. STRAP BRACING BOTH WAYS. NAILED TO PLATES") == \
        "DIAGONAL 30x0.8 G.I. STRAP BRACING BOTH WAYS"


def test_calc_sheet_drops_claude_bracing_item_and_long_basis():
    from app import calcs
    from app.schemas import CalcItem
    d = _calc_design()
    d.calculations = [CalcItem(topic="Racking / bracing check", basis="x", working="provided 30.6", result="ok"),
                      CalcItem(topic="Lintel L3", basis="tables", working="span 2.1 → L3 ≈ ok", result="140x45")]
    d.external_wall_type = "Brick veneer: 110 brick + cavity + 90 timber stud frame (250 overall). Vapour membrane."
    text = "\n".join(calcs.lines(GeoStub(), d, [], []))
    assert "PROVIDED 30.6" not in text and "LINTEL L3" in text
    assert "SPAN 2.1 -> L3 ~ OK" in text and "VAPOUR" not in text


def test_units_case_and_no_memory():
    from app.spelling import tidy
    assert tidy("-H(B) 6.00 KN/M (AS 1684.2 TABLE 8.18 (H) - TABLE NOT SUPPLIED, VALUE FROM MEMORY, CONFIRM)") == \
        "-H(B) 6.00 kN/m (AS 1684.2 TABLE 8.18 (H) - ENGINEER TO CONFIRM)"
    out = tidy("-WALLS 14.1 X 1.2 = 16.9 M2. 450W X 450 DEEP, 4/L11 TM, N25 (25 MPA), YS 20-40 MM, "
               "4/M12 CHEMSET, 2 M12 RODS, CLASS M, 0.8 KPA, 3.4 M SPAN, RACKING (KN)")
    assert "14.1 x 1.2 = 16.9 m²" in out and "450W x 450" in out and "25 MPa" in out and "20-40 mm" in out
    assert "4/M12" in out and "2 M12 RODS" in out and "CLASS M," in out and "0.8 kPa" in out
    assert "3.4 m SPAN" in out and "(kN)" in out and "N25" in out and "L11" in out


def test_units_superscripts_and_times():
    from app.spelling import tidy
    out = tidy("Z = 45 X 240² / 6 = 432,000 MM³; I = 51.8 X 10⁶ MM⁴; 2.40 m X 1.50 kN/m; 450W X MIN 400; "
               "AREA OF ELEVATION X PRESSURE; AS 1684.2 CLAUSE 2.6.? ROOF; TYPE X BRACING")
    assert "45 x 240²" in out and "432,000 mm³" in out and "10⁶ mm⁴" in out and "2.40 m x 1.50" in out
    assert "450W x MIN" in out and "ELEVATION x PRESSURE" in out and "CLAUSE 2.6 ROOF" in out
    assert "TYPE X BRACING" in out


def test_containment_backs_off_when_outline_wrong(monkeypatch):
    """Alterations (markup 25): the outline found is only the existing house, so most new-work
    lintels / bracing look 'outside' - nothing is moved or removed, a warning instead."""
    from app import pdf as P

    class FakeFP:
        ok = True

        def __init__(self, geo):
            pass

        def line_inside(self, p, q, tol=0):
            return p.x > 500  # only the 'existing house' on the right is inside

        def move_inside(self, p, q):
            raise AssertionError("must not move anything")

    monkeypatch.setattr(P, "Footprint", FakeFP)
    els = [Element(kind="bracing", tag="h(b) - 900", start=Point(x=0.1 * i, y=0.5), end=Point(x=0.1 * i, y=0.55), text="")
           for i in range(1, 5)] + [Element(kind="member", tag="L1", start=Point(x=0.9, y=0.2), end=Point(x=0.95, y=0.2), text="")]
    s = sheet(els)

    def pts(e):
        return pymupdf.Point(e.start.x * 1000, e.start.y * 1000), pymupdf.Point(e.end.x * 1000, e.end.y * 1000)

    w = P._contain(None, s, [4], [0, 1, 2, 3], pts, lambda i, r: None, "WALLS", None)
    assert len(s.elements) == 5 and "outline unclear" in w[0]


def test_wall_racking_note_matches_the_check():
    from app import calcs

    def br(tag, x1, y1, x2, y2):
        return Element(kind="bracing", tag=tag, start=Point(x=x1, y=y1), end=Point(x=x2, y=y2), text="")

    walls = MarkupSheet.model_construct(source_page_number=1, notes=[
        "-RACKING: WIND ON NORTH 11.9 KN, BRACING PROVIDED UP THE PAGE 19.3 KN.", "-ALL FRAMING TO AS 1684.2."],
        elements=[br("h(b) - 900", 0.2, 0.2, 0.2, 0.25), br("d - 2400", 0.3, 0.5, 0.4, 0.5)])
    notes = calcs.racking_notes(_calc_design(force_up=11.9, force_across=2.0), walls, 1.414)
    assert "-ALL FRAMING TO AS 1684.2." in notes and not any("19.3" in n for n in notes)
    assert "11.9 kN REQUIRED, 5.4 kN PROVIDED (NOT ADEQUATE - ADD BRACING)" in notes[-1]
    assert "REFER CALCULATION SHEET" in notes[-1]


def test_brief_keeps_ranges():
    from app.calcs import _brief
    assert _brief("0 - 20 mm (reactivity equivalent to class S)") == "0 - 20 mm"
    assert _brief("clad timber frame - weatherboard") == "clad timber frame"


def test_scale_for_paper_size_and_claude_hint():
    """1:100 @ A1 printed on an A3 page is 1:200 on the page; Claude's reading is used when the
    sheet's text can't be read (markup 26: piers drawn twice their size at a guessed 1:100)."""
    from app.snap import drawing_scale, scale_for_page
    doc = pymupdf.open()
    a3 = doc.new_page(width=1190.55, height=841.89)
    assert scale_for_page("SCALE 1:100 @ A1", a3) == [200]
    assert scale_for_page("1:100 @ A3", a3) == [100] and scale_for_page("1:50", a3) == [50]
    assert drawing_scale(a3) == (None, "scale not found")
    doc.scale_hints = {0: "1:100 @ A1"}
    assert drawing_scale(doc[0])[0] == 200
    a3.insert_text((50, 50), "SCALE 1:100")  # plain on the sheet: the reading with the paper wins
    assert drawing_scale(doc[0])[0] == 200
    doc.scale_hints = {}
    assert drawing_scale(doc[0])[0] == 100


def test_ledgers_retagged_ld():
    from app.pipeline import ledger_tags
    s = sheet([Element(kind="member", tag="L5", start=Point(x=0.1, y=0.1), end=Point(x=0.2, y=0.1), text=""),
               Element(kind="member", tag="L1", start=Point(x=0.3, y=0.1), end=Point(x=0.4, y=0.1), text=""),
               Element(kind="callout", tag="", start=Point(x=0.1, y=0.1), end=Point(x=0.1, y=0.1),
                       text="L5 LEDGER BOLTED TO EXISTING WALL. R2 TO L5 WITH HANGERS. L1 OVER DOOR")],
              [ScheduleItem(tag="L5", text="140x45 F17/LVL LEDGER"), ScheduleItem(tag="L1", text="90x35 F17/LVL LINTEL"),
               ScheduleItem(tag="LD5", text="190x45 F7 TP DECK LEDGER")])
    w = ledger_tags(s, "ROOF")
    assert [x.tag for x in s.schedule] == ["LD6", "L1", "LD5"]  # LD5 taken: next free
    assert [e.tag for e in s.elements][:2] == ["LD6", "L1"]
    assert s.elements[2].text == "LD6 LEDGER BOLTED TO EXISTING WALL. R2 TO LD6 WITH HANGERS. L1 OVER DOOR"
    assert "L5 -> LD6" in w[0]
