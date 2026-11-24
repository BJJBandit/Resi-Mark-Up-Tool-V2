

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
