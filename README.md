# Resi Mark-Up Tool

Web app that prepares **preliminary** structural mark-ups for Class 1 and 10 residential
projects to the NCC and Australian Standards. All output is a design aid for the engineer
and must be checked before use. It is not for construction.

## How it works (v2)

Two stages, with the engineer in between:

1. **Model** – the architectural PDF is read deterministically: vector line work,
   dimension strings and scale give walls, openings and plan frames; title-block text
   gives site class, wind class and BAL. Claude (Sonnet) is then asked only what needs
   judgement: which internal walls are load-bearing (by wall ID, on a plan image with the
   IDs drawn on it), the roof form, and what the geotech report says. The result is a
   structural model you **review and correct in the browser** before anything is designed.
2. **Design** – no tokens. A fixed dependency graph decides which engines run (AS 4055
   wind → AS 1684 racking / tie-down; AS 2870 footings → piers; openings → lintels →
   AS 4100 steel). Each engine walks a YAML decision tree over tables transcribed from
   the standards, records every step in a **design trace** with its citation, and the
   layout rules place footings, piers, bracing panels and tags on the plan. The sheets
   are drawn as PDF annotations so they stay editable in Bluebeam / Acrobat.

Standards tables ship **empty**: values come from the practice's licensed copies (see
`docs/standards-data-required.md`). Until a table is loaded the engine proposes the
practice standard from `app/data/house/preferences.yaml`, marks it unverified, and says
exactly which table it needs. Where a loaded table is more conservative than the
preference, the table wins and the trace says so.

`docs/architecture.md` has the module map; `/design-logic` in the running app shows
every decision tree and the loaded / not-loaded state of every table.

A **legacy mode** (Claude reads page images, as in v1) remains for scanned drawing sets.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload
pytest -q           # 50+ unit tests; fixture tests skip when PDFs are absent
```

Then open http://localhost:8000. Past jobs for regression testing go in `fixtures/`
(see `fixtures/README.md`).

## Deploy on Render

`render.yaml` is a Render Blueprint. In Render, choose **New → Blueprint** and pick this
repo. When Render asks for `ANTHROPIC_API_KEY`, paste in your key.

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none, secret) | Claude API key |
| `ANTHROPIC_MODEL_LABEL` | `claude-sonnet-5-5` | Model for the labelling stage (v2) |
| `ANTHROPIC_EFFORT_LABEL` | `medium` | Reasoning effort for labelling |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model for legacy mode |
| `ANTHROPIC_EFFORT` | `high` | Reasoning effort for legacy mode |
| `TITLE_BLOCK_COMPANY` | `ARETE` | Company name in the title block |
| `TITLE_BLOCK_COMPANY_SUB` | `ENGINEERING` | Second line under the company name |
| `MARKUP_AUTHOR` | `Resi Mark-Up Tool` | Author shown on every annotation in Bluebeam's markup list |

## Limits

- Uploads must be PDFs under 30 MB; the model stage needs vector drawings (CAD / Revit
  exports). Image-only pages are flagged; use legacy mode for those.
- Jobs are kept in memory only, so download the results before the service restarts.

## Code layout

| Path | Purpose |
|---|---|
| `app/main.py` | Routes: upload, model review, design, outputs |
| `app/model/` | The structural model schema (reviewable JSON with provenance) |
| `app/extract/` | Deterministic PDF geometry: vectors, dimensions, scale, walls, openings |
| `app/label/` | Claude labelling (load-bearing walls, roof, geotech) and roof-text parsing |
| `app/design/` | Planner (dependency graph), runner, layout, annotator |
| `app/engines/` | Standards engines, tables loader, trace, YAML decision-tree interpreter |
| `app/logic/` | Decision trees as YAML, rendered on `/design-logic` |
| `app/calc/` | AS 4100 beam / column checks and the steel section catalogue |
| `app/data/house/` | Practice preferences and notes library (editable data) |
| `app/data/standards/` | Standards tables (transcribed by the engineer) |
| `app/pdf.py` | Draws the A3 mark-up sheets as PDF annotations |
| `app/pipeline.py`, `app/claude.py`, `app/schemas.py` | Legacy v1 pipeline |
| `app/static/` | Upload, review and trace pages |
| `docs/` | Standards data checklist, extracted house style, architecture |
| `fixtures/` | Past jobs as regression tests (manifest committed, PDFs synced) |
