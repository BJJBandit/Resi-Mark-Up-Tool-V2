

def test_no_notes_on_markup_sheets_they_go_after_calc():
    """Mark-up sheets carry only the plan, callouts and schedules; the notes follow the calc sheet."""
    import inspect
    from app import pdf
    src = inspect.getsource(pdf._markup_sheet)
    assert "_notes(page" not in src
    body = inspect.getsource(pdf._build_markup_pdf)
    assert body.index("_calc_sheet(") < body.index("_sheet_notes(out")
    assert pdf.PLAN.x0 < 50  # the plan takes the old notes column


def test_title_fields_typed_in_the_tool_fill_sheets_and_notes():
    import pymupdf
    from app import pdf as P
    from app.main import _title_fields
    from app.schemas import Box, MarkupSheet
    from app.schemas import SheetKind as K
    typed = _title_fields('{"ADDRESS": "12 Smith St", "CHECKED BY": "jb", "ISSUE DESCRIPTION - 2": "for approval",'
                          ' "ISSUE REVISION - 2": "b", "DRAWING STATUS": "for construction", "BOGUS": "x"}')
    assert "BOGUS" not in typed and typed["CHECKED BY"] == "JB"
    src = pymupdf.open()
    src.new_page(width=1190.55, height=841.89)
    s = MarkupSheet.model_construct(source_page_number=1, plan_region=Box(x1=0, y1=0, x2=1, y2=1), elements=[],
                                    schedule=[], legend=[], notes=[], classification="",
                                    schedule_title="", legend_title="")
    out, _ = P.build_markup_pdf(src.tobytes(), "1 TEST ST", None, None, {K.WALLS: s}, job_no="J1", rev="A",
                                made_by="DM", calc_sheet=False, arete_title_block=True, notes_sheets=True,
                                title_fields=typed)
    doc = pymupdf.open(stream=out)
    notes = {w.field_name: w.field_value for w in doc[0].widgets()}  # S01-01 first
    assert notes["ADDRESS"] == "12 SMITH ST" and notes["CHECKED BY"] == "JB"
    assert notes["ISSUE DESCRIPTION - 2 (1)"] == "FOR APPROVAL" and notes["ISSUE DESCRIPTION - 1"] == "PRELIMINARY MARK-UP"
    sheet = next(p for p in doc if any((w.field_name or "").endswith("(SSK-1)") for w in p.widgets()))
    tb = {w.field_name: w.field_value for w in sheet.widgets()}
    assert tb["DRAWING STATUS (SSK-1)"] == "FOR CONSTRUCTION" and tb["ISSUE REVISION - 2 (SSK-1)"] == "B"
    assert tb["PROJECT NUMBER (SSK-1)"] == "J1"  # untouched defaults stay


def test_members_follow_an_out_of_square_wall():
    """A wall drawn 1.3 degrees off square: the footing along it follows its angle, the bearer
    ending on it and the pier on it move onto it; a member not near it stays put."""
    import math
    import pymupdf
    from app import pdf as P
    from app.schemas import Element, Point
    from app.snap import Geometry, skew_walls
    doc = pymupdf.open()
    pg = doc.new_page(width=1190, height=842)
    for k in range(260):  # square line work so the page counts as a vector drawing
        pg.draw_line((100 + k, 700), (100 + k, 720))
    t = math.tan(math.radians(-1.3))
    for off in (0, 4):  # the two faces of the rear wall
        pg.draw_line((250, 198 + off), (665, 198 + off + 415 * t))
    pg.draw_line((300, 400), (305, 400.1))  # short off-square bit, ignored
    geo = Geometry(pg)
    (a, b), = skew_walls(geo)
    assert abs(math.degrees(math.atan2(b.y - a.y, b.x - a.x)) + 1.3) < 0.05
    w, h = pg.rect.width, pg.rect.height
    els = [Element(kind=k, tag=tg, start=Point(x=x1 / w, y=y1 / h), end=Point(x=x2 / w, y=y2 / h), text="")
           for k, tg, x1, y1, x2, y2 in [("member", "SF1", 252, 195, 660, 195), ("member", "B1", 450, 195, 450, 500),
                                          ("pier", "EP1", 450, 195, 450, 195), ("member", "J1", 252, 330, 650, 330)]]
    s = type("S", (), {})()
    s.elements = els
    pts = lambda e: (pymupdf.Point(e.start.x * w, e.start.y * h), pymupdf.Point(e.end.x * w, e.end.y * h))

    def put(i, r):
        s.elements[i] = s.elements[i].model_copy(update={"start": Point(x=r[0].x / w, y=r[0].y / h),
                                                         "end": Point(x=r[1].x / w, y=r[1].y / h)})
    assert P._follow_skewed_walls(geo, s, pts, put) == 3
    centre = lambda x: 200 + (x - 250) * t
    sf, bearer, pier, joist = (pts(e) for e in s.elements)
    assert abs(sf[0].y - centre(252)) < 0.3 and abs(sf[1].y - centre(660)) < 0.3  # along the wall, at its angle
    assert abs(bearer[0].y - centre(450)) < 0.3 and bearer[1].y == 500  # end on the wall, other end kept
    assert abs(pier[0].y - centre(450)) < 0.3
    assert joist[0].y == 330 and joist[1].y == 330


def _sheet(els, schedule=()):
    from app.schemas import Box, MarkupSheet, ScheduleItem
    return MarkupSheet.model_construct(source_page_number=1, plan_region=Box(x1=0, y1=0, x2=1, y2=1),
                                       elements=els, schedule=[ScheduleItem(tag=t, text=x) for t, x in schedule],
                                       legend=[], notes=[], classification="", schedule_title="", legend_title="")


def _el(kind, tag, x1, y1, x2=None, y2=None, w=1190.0, h=842.0):
    from app.schemas import Element, Point
    x2 = x1 if x2 is None else x2
    y2 = y1 if y2 is None else y2
    return Element(kind=kind, tag=tag, start=Point(x=x1 / w, y=y1 / h), end=Point(x=x2 / w, y=y2 / h), text="")


def test_end_bay_wider_than_bearer_spacing_is_named():
    import pymupdf
    from app import pdf as P
    from app.snap import mm_to_pt
    pg = pymupdf.open().new_page(width=1190, height=842)
    s18, x0 = mm_to_pt(1800, 100), 100.0
    xs = [x0 + s18 * k for k in (1, 2, 3)]
    els = [_el("member", "SF1", x0, 100, x0, 400), _el("member", "SF1", xs[-1] + mm_to_pt(2200, 100), 100,
                                                            xs[-1] + mm_to_pt(2200, 100), 400)]
    els += [_el("member", "B1", x, 100, x, 400) for x in xs]
    out = P._spacing_check(_sheet(els, [("B1", "2/240x45 F7 BEARER AT 1800 CTRS MAX")]), pg, 100, "FLOOR")
    assert len(out) == 1 and "end bay" in out[0] and "2200 mm" in out[0]


def test_beam_span_longer_than_its_calculation_is_named():
    import pymupdf
    from app import pdf as P
    from app.schemas import CalcItem
    from app.snap import mm_to_pt
    pg = pymupdf.open().new_page(width=1190, height=842)
    design = type("D", (), {"calculations": [CalcItem(topic="DECK BEAM VB1 240x45 F17", basis="SPAN 2.8 m BETWEEN C1",
                                                      working="", result="OK")]})()
    spans = P._calc_spans(design)
    assert spans == {"VB1": 2800.0}
    L = mm_to_pt(7600, 100)
    els = [_el("member", "VB1", 100, 100, 100, 100 + L), _el("column", "C1", 100, 100 + L / 2)]  # 3.8 m spans
    out = P._beam_span_check(_sheet(els), pg, 100, "ROOF", spans)
    assert len(out) == 1 and "3.8 m" in out[0] and "2.8 m" in out[0]
    els.append(_el("column", "C1", 100, 100 + L / 4))
    els.append(_el("column", "C1", 100, 100 + 3 * L / 4))
    assert P._beam_span_check(_sheet(els), pg, 100, "ROOF", spans) == []  # 1.9 m spans


def test_columns_match_the_footing_sheet():
    from app import pdf as P
    footing = [_el("column", "C1", 100, 200), _el("column", "C1", 100, 300), _el("column", "C1", 100, 400)]
    roof = _sheet([_el("member", "VB1", 100, 100, 100, 400), _el("column", "C1", 100, 260), _el("column", "C1", 100, 400)])
    w = P._sync_columns(roof, 0, (0, footing, "FOOTING & FLOOR PLAN"), "ROOF FRAMING PLAN")
    assert w and "C1 (2 -> 3)" in w[0]
    ys = sorted(round(e.start.y * 842) for e in roof.elements if e.kind == "column")
    assert ys == [200, 300, 400]
    assert P._sync_columns(roof, 1, (0, footing, "FOOTING & FLOOR PLAN"), "ROOF FRAMING PLAN") == []  # other page


def test_timber_lintel_over_a_steel_one_is_dropped():
    import pymupdf
    from app import pdf as P
    els = [_el("member", "L1", 688, 341, 688, 389), _el("member", "L6", 688, 341, 688, 389),
           _el("member", "L2", 276, 263, 358, 263), _el("member", "L3", 250, 256, 375, 256)]
    s = _sheet(els, [("L6", "2/100x75x8 UA LINTEL"), ("L1", "90x35 F17/LVL LINTEL"),
                     ("L2", "120x35 F17/LVL LINTEL"), ("L3", "140x45 F17/LVL LINTEL")])
    pts = lambda e: (pymupdf.Point(e.start.x * 1190, e.start.y * 842), pymupdf.Point(e.end.x * 1190, e.end.y * 842))
    out = P._lintel_checks(None, s, pts, "WALLS")
    assert [e.tag for e in s.elements] == ["L6", "L2", "L3"]
    assert any("L1 removed" in o for o in out) and any("L2 and L3 overlap" in o for o in out)


def test_bearers_along_the_base_wall_are_removed_or_trimmed():
    """markup 9: B1 drawn 4-5 pt beside the perimeter SF1 at the laundry west wall (removed), the east
    wall (removed) and down the D01 line past the laundry / kitchen wall (trimmed to the internal
    wall part); bearers clear of the footings stay."""
    import pymupdf
    from app import pdf as P
    sf = [(250, 198, 675, 188), (675, 188, 675, 514), (373, 514, 675, 514), (373, 275, 373, 514),
          (373, 275, 250, 275), (250, 198, 250, 275)]
    els = [_el("member", "SF1", *c) for c in sf]
    els += [_el("member", "B1", *c) for c in [(245, 198, 245, 275), (313, 197, 313, 275), (377, 195, 377, 517),
                                              (445, 194, 445, 516), (670, 189, 670, 514)]]
    s = _sheet(els)
    pts = lambda e: (pymupdf.Point(e.start.x * 1190, e.start.y * 842), pymupdf.Point(e.end.x * 1190, e.end.y * 842))

    def put(i, r):
        from app.schemas import Point
        s.elements[i] = s.elements[i].model_copy(update={"start": Point(x=r[0].x / 1190, y=r[0].y / 842),
                                                         "end": Point(x=r[1].x / 1190, y=r[1].y / 842)})
    out = P._bearers_off_base_walls(s, pts, put, "FOOTINGS")
    bearers = sorted((round(pts(e)[0].x), round(pts(e)[0].y), round(pts(e)[1].y)) for e in s.elements if e.tag == "B1")
    assert bearers == [(313, 197, 275), (377, 195, 275), (445, 194, 516)]
    assert any("removed" in o for o in out) and any("trimmed" in o for o in out)


def test_separate_floor_sheet_bearers_checked_against_footing_sheet():
    import pymupdf
    from app import pdf as P
    pg = pymupdf.open().new_page(width=1190, height=842)
    footings = [_el("member", "SF1", *c) for c in [(250, 198, 675, 188), (675, 188, 675, 514), (250, 198, 250, 275)]]
    floor = _sheet([_el("member", "B1", 245, 198, 245, 275), _el("member", "B1", 445, 194, 445, 516),
                    _el("member", "B1", 670, 189, 670, 514), _el("member", "J1", 250, 300, 670, 300)])
    out = P._floor_bearers_vs_footings(floor, pg, 0, (0, footings), "FLOOR FRAMING PLAN")
    assert [round(e.start.x * 1190) for e in floor.elements] == [445, 250]  # B1 at 445 and the joist kept
    assert not any(e.tag == "SF1" for e in floor.elements)  # borrowed footings not drawn
    assert out and "footing sheet" in out[0]
    assert P._floor_bearers_vs_footings(_sheet([_el("member", "B1", 245, 198, 245, 275)]), pg, 1, (0, footings),
                                        "FLOOR FRAMING PLAN") == []  # other page


def test_wet_area_joists_are_not_stretched_wall_to_wall():
    from app import pdf as P
    s = _sheet([], [("J1", "140x45 F7 TP CONTINUOUS JOIST AT 450 CTRS"),
                    ("J3", "140x45 F7 TP CONTINUOUS JOIST AT 350 CTRS UNDER WET / TILED AREAS")])
    assert P._wet_joist_tags(s) == {"J3"}
    assert P._wet_joist_tags(_sheet([], [("J1", "140x45 F7 TP JOIST AT 450 CTRS UNDER BATH")])) == set()  # only one


def test_bracing_over_a_lintel_moves_onto_solid_wall():
    import pymupdf
    from app import pdf as P
    from app.snap import Geometry
    pg = pymupdf.open().new_page(width=1190, height=842)
    for y in (500, 504):  # wall faces with a window gap 380-420
        pg.draw_line((300, y), (380, y))
        pg.draw_line((420, y), (600, y))
    geo = Geometry(pg)
    s = _sheet([_el("member", "L1", 380, 502, 420, 502), _el("bracing", "g - 900", 380, 503, 420, 503)])
    pts = lambda e: (pymupdf.Point(e.start.x * 1190, e.start.y * 842), pymupdf.Point(e.end.x * 1190, e.end.y * 842))

    def put(i, r):
        from app.schemas import Point
        s.elements[i] = s.elements[i].model_copy(update={"start": Point(x=r[0].x / 1190, y=r[0].y / 842),
                                                         "end": Point(x=r[1].x / 1190, y=r[1].y / 842)})
    out = P._bracing_off_lintels(geo, s, pts, put, "WALLS")
    a, b = pts(s.elements[1])
    assert out and "moved" in out[0]
    assert (round(a.x), round(b.x)) == (339, 379)  # nearest side with solid wall, clear of the lintel


def test_tall_brick_pier_drawn_at_its_350_base_and_600_pier():
    from app.pdf import _sizes
    assert _sizes("230x230 PIER MAX 1200 HIGH ON 350x350 PIER, MAX 2400 TOTAL, ON P2 600 DIA MASS CONCRETE "
                  "BORED PIER") == (600.0, 350.0)
    assert _sizes("230x230 BRICK PIER TO MAX 1500 HIGH ON P1 450 DIA BORED PIER") == (450.0, 230.0)


def test_strip_footing_under_an_existing_wall_moves_beside_it():
    import pymupdf
    from app import pdf as P
    pg = pymupdf.open().new_page(width=1190, height=842)
    pg.draw_rect(pymupdf.Rect(664, 178, 670, 525), color=None, fill=(0.7, 0.7, 0.7))  # existing wall, grey fill
    sf = [(277, 192, 667, 183), (667, 183, 667, 481), (391, 481, 667, 481), (391, 192, 391, 481)]
    els = [_el("member", "SF1", *c) for c in sf] + [_el("pier", "EP1", 667, 300), _el("pier", "EP1", 500, 481)]
    s = _sheet(els, [("SF1", "450W x 450 DEEP STRIP FOOTING")])
    pts = lambda e: (pymupdf.Point(e.start.x * 1190, e.start.y * 842), pymupdf.Point(e.end.x * 1190, e.end.y * 842))
    out = P._footings_off_existing_walls(pg, s, pts, "FOOTINGS")
    sfs = [pts(e) for e in s.elements if e.tag == "SF1"]
    east = next(l for l in sfs if abs(l[0].x - l[1].x) < 0.1 and l[0].x > 600)
    assert len(sfs) == 4  # kept
    assert 655 <= east[0].x < 664  # parallel, beside the wall on the new side
    north = next(l for l in sfs if abs(l[0].y - l[1].y) > 1 or l[0].y < 200)
    assert abs(max(north[0].x, north[1].x) - east[0].x) < 0.2  # the north footing's end follows it
    ep = next(pts(e)[0] for e in s.elements if e.tag == "EP1" and pts(e)[0].y < 400)
    assert abs(ep.x - east[0].x) < 0.2  # its engaged pier too
    assert out and "parallel" in out[0]
    # Every wall filled grey (new ones too): nothing moves.
    for x in (277, 391):
        pg.draw_rect(pymupdf.Rect(x - 3, 180, x + 3, 485), color=None, fill=(0.7, 0.7, 0.7))
    pg.draw_rect(pymupdf.Rect(270, 478, 670, 484), color=None, fill=(0.7, 0.7, 0.7))
    s2 = _sheet([_el("member", "SF1", *c) for c in sf])
    assert P._footings_off_existing_walls(pg, s2, pts, "FOOTINGS") == []


def test_lintel_inside_the_existing_building_is_removed():
    import pymupdf
    from app import pdf as P
    pg = pymupdf.open().new_page(width=1190, height=842)
    g = (0.7, 0.7, 0.7)
    for r in [(513, 270, 519, 333), (513, 369, 519, 520),  # existing wall the new work joins (opening 333-369)
              (814, 381, 819, 410), (814, 446, 819, 523)]:  # existing internal wall deep in the house
        pg.draw_rect(pymupdf.Rect(*r), color=None, fill=g)
    s = _sheet([_el("member", "L1", 474, 378, 508, 378), _el("member", "B1", 480, 200, 480, 520),
                _el("member", "L6", 516, 336, 516, 371), _el("member", "L6", 816, 407, 816, 449)])
    pts = lambda e: (pymupdf.Point(e.start.x * 1190, e.start.y * 842), pymupdf.Point(e.end.x * 1190, e.end.y * 842))
    out = P._lintels_off_existing(pg, s, pts, "WALLS")
    assert [round(e.start.x * 1190) for e in s.elements if e.tag == "L6"] == [516]  # the junction opening stays
    assert out and "existing building" in out[0]



def test_strip_footing_set_back_from_an_existing_wall_moves_beside_it_round_a_chimney():
    """markup 53: the link east SF drawn about 1 m in from the existing west wall - it runs right
    beside the wall, stepping round the chimney breast (drawn as an outline, hollow between)."""
    import pymupdf
    from app import pdf as P
    pg = pymupdf.open().new_page(width=1190, height=842)
    g = (0.7, 0.7, 0.7)
    for r in [(677, 170, 683, 424), (658, 424, 702, 432), (677, 432, 683, 468), (658, 468, 702, 477),
              (677, 477, 683, 523)]:
        pg.draw_rect(pymupdf.Rect(*r), color=None, fill=g)
    sf = [(288, 188, 639, 180), (639, 180, 639, 479), (406, 479, 639, 479), (406, 185, 406, 479)]
    s = _sheet([_el("member", "SF1", *c) for c in sf] + [_el("pier", "EP1", 639, 300)],
               [("SF1", "450W x 450 DEEP STRIP FOOTING")])
    pts = lambda e: (pymupdf.Point(e.start.x * 1190, e.start.y * 842), pymupdf.Point(e.end.x * 1190, e.end.y * 842))
    out = P._footings_off_existing_walls(pg, s, pts, "FOOTINGS")
    assert out and "beside" in out[0]
    xs = sorted({round(pts(e)[0].x) for e in s.elements if e.tag == "SF1" and abs(pts(e)[0].x - pts(e)[1].x) < 0.1
                 and pts(e)[0].x > 600})
    assert xs == [652, 671]  # beside the wall face (677) and stepped out round the chimney (658)
    assert round(pts(next(e for e in s.elements if e.tag == "EP1"))[0].x) == 671
