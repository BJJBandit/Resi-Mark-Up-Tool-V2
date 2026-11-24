

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
