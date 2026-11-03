# Resi Mark-Up Tool

Web app that uses the Claude API to prepare **preliminary** structural mark-ups for
residential projects. All output is a design aid for the engineer and must be
checked before use. It is not for construction.

## What it does

1. **Geotechnical review and wind.** Reads the geotech report (PDF) and records the
   AS 2870 site class, ys, bearing capacity, founding depth, fill, groundwater and the
   report's recommendations. Uses the AS 4055 wind class you enter, or the one in the report.
2. **Architectural review.** Reads every drawing page as an image plus its extracted text, then works out:
   - roof type
   - wall type
   - load paths
   - footing type, based on the geotech report and AS 2870
   - wall design, including bracing, to the material standard you choose
     (AS 1684, NASH, AS 3700 / AS 4773)
   - Optionally takes your own **standards, guides and codes** (PDFs, several at once).
     Every step reads them and cites their clause and table numbers instead of relying
     on memory. They are uploaded once per review through the Files API and deleted
     when the review finishes.
3. **Mark-up drawings.** Produces one PDF with:
   - a summary page
   - one marked-up sheet each for load paths, roof (rafters only, including the deck),
     wall framing / racking / blockwork joints, footings (AS 2870) and floor framing or slab

   Sheets follow the house mark-up style: A3, address and sheet name top-left,
   classification and notes down the left, the architectural plan faded in the middle
   with colour-coded member lines and tags (B, J, L, VB, R, C, DS, EB ...), piers,
   columns, bracing panels (`h(b) - 900`, `g - 600`, dashed `d - 2400`), boxed red
   callouts with arrow leaders, the schedule / bracing legend on the right and the title
   block (Job No, Dwg No SSK-x, Rev, Date, Made by) bottom-right.

   **Everything added is a PDF annotation** (title, notes, schedules, title block,
   members, tags, callouts), so the whole mark-up can be moved, edited or deleted in
   Bluebeam or Acrobat. Only the faded architectural background is page content.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload
```

Then open http://localhost:8000.

## Deploy on Render

`render.yaml` is a Render Blueprint. In Render, choose **New → Blueprint** and pick
this repo. When Render asks for `ANTHROPIC_API_KEY`, paste in your key.

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none, secret) | Claude API key |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model used for every step |
| `ANTHROPIC_EFFORT` | `high` | Reasoning depth: `low` / `medium` / `high` / `xhigh` / `max` |
| `TITLE_BLOCK_COMPANY` | `ARETE` | Company name in the title block |
| `TITLE_BLOCK_COMPANY_SUB` | `ENGINEERING` | Second line under the company name |
| `MARKUP_AUTHOR` | `Resi Mark-Up Tool` | Author shown on every annotation in Bluebeam's markup list |

## Limits

- Architectural sets can have up to 20 pages, and each upload must be under 30 MB.
- Reference documents can be up to 100 MB each and 500 pages in total. Every page is
  read on every step, so upload only the relevant chapters (for example AS 1684.2
  bracing and tie-down, and the AS 2870 footing tables). That keeps the review faster
  and cheaper.
- Mark-up positions come from Claude reading the page image. They are approximate,
  so check them and drag them into place in your PDF editor.
- Jobs are kept in memory only, so download the results before the service restarts
  or redeploys.

## Code layout

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI routes and the background job runner |
| `app/pipeline.py` | Runs steps 1 to 3 in order |
| `app/claude.py` | Prompts and Claude API calls (structured outputs) |
| `app/schemas.py` | Output schema for each step |
| `app/pdf.py` | Renders pages and draws the mark-up PDF |
| `app/static/index.html` | Upload page |
