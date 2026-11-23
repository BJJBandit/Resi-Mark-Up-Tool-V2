

def test_no_notes_on_markup_sheets_they_go_after_calc():
    """Mark-up sheets carry only the plan, callouts and schedules; the notes follow the calc sheet."""
    import inspect
    from app import pdf
    src = inspect.getsource(pdf._markup_sheet)
    assert "_notes(page" not in src
    body = inspect.getsource(pdf._build_markup_pdf)
    assert body.index("_calc_sheet(") < body.index("_sheet_notes(out")
    assert pdf.PLAN.x0 < 50  # the plan takes the old notes column
