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
