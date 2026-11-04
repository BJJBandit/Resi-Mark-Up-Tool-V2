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
   - one marked-up sheet each for load paths, roof (rafters only, including the deck),
     wall framing / racking / blockwork joints, footings (AS 2870) and floor framing or slab

   Sheets follow the house mark-up style: A3, address and sheet name top-left,
   classification and notes down the left, the architectural plan faded in the middle
   with colour-coded member lines and tags (B, J, L, VB, R, C, DS, EB ...), piers,
   columns, bracing panels (`h(b) - 900`, `g - 600`, dashed `d - 2400`), boxed red
   callouts with arrow leaders, the schedule / bracing legend on the right and the title
   block bottom-right if you tick **Add title block** (Job No, Dwg No SSK-x, Rev, Date,
   Made by). Without it the schedule and callouts use that space.

   The review summary (geotech, roof/wall/footing assessment, engineer checks) is shown
   in the app and in `review.json`; it is not added to the PDF.

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
| `ANTHROPIC_EFFORT` | `high` | Reasoning depth for steps 1-2 (geotech + architectural review): `low` / `medium` / `high` / `xhigh` / `max` |
| `ANTHROPIC_SHEET_EFFORT` | `medium` | Reasoning depth for step 3 (placing the mark-ups on each sheet) |
| `ANTHROPIC_TIMEOUT_S` | `1800` | Max seconds a Claude stream may sit silent before it is retried once |
| `ANTHROPIC_MAX_TOKENS` | `64000` | Output cap per Claude call (thinking counts toward it); a cut-off call retries once at 128000 |
| `LINTEL_MAX_SPAN_DEPTH` | `20` | Lintel check: flag a lintel whose clear span exceeds this many times its depth |
| `BEAM_WIDTH_FROM_MM` / `BEAM_WIDTH_TO_MM` | `300` / `450` | Edge / internal beam width rule |
| `MIN_STRIP_FOOTING_WIDTH_MM` | `450` | Firm minimum strip footing width (see Design rules) |
| `TITLE_BLOCK_COMPANY` | `ARETE` | Company name in the title block |
| `TITLE_BLOCK_COMPANY_SUB` | `ENGINEERING` | Second line under the company name |
| `MARKUP_AUTHOR` | `Resi Mark-Up Tool` | Author shown on every annotation in Bluebeam's markup list |

## Design rules

Firm rules are given to Claude **and** enforced on its output (`app/rules.py`), so they
hold even if Claude slips:

- **Strip footings are at least 450 mm wide.** Any strip footing width below the minimum
  in the design assessment, schedules, callouts or notes is raised to 450 mm, and each
  change is listed as a warning on the results page. Internal beams, edge beams, piers and
  depths are not touched. Narrower strip footings recommended in the geotech report are
  flagged, not rewritten, because the report is quoted as written.
- **Edge / internal / stiffening beams that AS 2870 gives as 300 mm wide are made 450 mm
  wide** (300 + 150). Applied to the design assessment, schedules (EB / IB tags),
  callouts and notes, with a warning per change; other widths, depths, bar spacings,
  pads and piers are not touched; a 300 wide beam in the geotech report is flagged.

## Limits

- Architectural sets can have up to 60 pages (`MAX_DRAWING_PAGES`), and each upload must be under 30 MB. Use **Drawing pages to review** (e.g. `3-12, 15`) to skip covers, 3D renders and BASIX pages: fewer pages is faster and cheaper. Large sets are sent at a lower image resolution so they fit in one request.
- Reference documents (standards, guides, codes) can be up to 100 MB each. Leave a
  document's **Pages to review** blank and the app reads its bookmarks, contents pages and
  the first lines of every page, asks Claude which sections this job needs (footings,
  bracing, tie-down, span tables ...), and sends only those pages (`app/refpages.py`).
  The results page lists, for every document, the pages used, the sections they cover and
  why. Type a range (e.g. `45-60, 72`) to choose yourself, or `all` for the whole document.
  Documents of 25 pages or less are sent whole. If a long document's contents can't be
  read (a scan) or no relevant section is found, it is not sent and a warning asks for its
  pages. The pages sent across all documents total at most 500.
- Mark-up positions come from Claude reading the page image, so they are approximate;
  check them and drag them into place in your PDF editor.
- **Footings, bearers and beams are snapped to the walls** in a CAD-exported drawing:
  edge beams / strip footings / internal beams (EB, SF, IB) go on the wall centreline, and
  bearers / beams / ledgers (B, VB, RB, LD) too, or about 3 pt beside a footing line
  already on that wall (house style, so both lines show). A member only snaps when a wall
  runs along at least half its length, so bearers spanning mid-room and deck beams stay
  as placed. Ends snap to the crossing walls, so footing corners close. Footing lines
  that are not on any wall are named in the warnings.
- **Bearers are snapped to their footings and piers** on the floor sheet too: a bearer
  that is not on a wall but runs along a footing line (EB / SF / IB) is drawn 3 pt beside
  that footing on the side Claude placed it; a mid-room bearer over a row of piers /
  columns (2 or more within 6 pt) goes onto the line through them (averaged with its own
  position), and the piers are then snapped back onto it.
- **Lintel check on the wall framing sheet** (`app/lintel_check.py`), after snapping. The
  architect's window / door codes (height then width: A1812 = 1800 high x 1200 wide)
  locate every opening. A red cloud and a warning are added for (1) an opening with no
  lintel on a wall that has other lintels (so it is load-bearing and in scope - existing
  walls on alteration jobs are not flagged unless the tool lintelled that wall), (2) a
  lintel smaller than one over a narrower opening on the same wall, (3) a lintel spanning
  more than `LINTEL_MAX_SPAN_DEPTH` (20) times its depth. Continuous lintels are skipped.
  This is a consistency check - it does not size lintels to AS 1684.2.
- **Piers and columns are snapped to what they support**: onto the end of a member or
  the crossing of two lines (member / wall) if one is within 5 pt, otherwise straight onto
  the nearest member (bearer, beam, edge beam, verandah beam ...) or wall line within 6 pt,
  keeping their spacing along it. Points with nothing nearby stay put. Runs last, after
  the members have snapped.
- **Joists and rafters are snapped to their supports**: each end of a joist / rafter
  (J, R, T tags - span arrows and representative members) moves onto the wall, bearer,
  beam, ledger, lintel or footing it bears on (walls from the drawing, the rest from the
  same sheet after they have snapped). The line keeps its position across the room;
  an end with no support nearby stays put. Decking, stair treads and joinery are not
  mistaken for walls.
- **Bracing panels are snapped to their walls**: drawn about 3 pt beside the wall
  centreline (house style, on the side Claude placed them), at the exact scaled length from
  the tag (`h(b) - 900` -> 900 mm) using the plan scale printed in the drawing set or, if
  none is printed, inferred from the 90 mm stud walls; and slid along the wall off any
  window (glazing line) or door (gap in the wall faces). Panels not on a wall, or with no
  solid wall long enough nearby, are left as placed and named in the warnings.
- **Lintels are snapped to the openings** when the architectural PDF is a CAD export
  (vector line work): each lintel is moved onto the wall centreline, from jamb to jamb
  plus about one wall thickness of bearing each side (`app/snap.py`). Where no clear
  opening is found, or two openings fit equally well, the lintel is left as Claude
  placed it and named in the warnings. Scanned drawings have no vectors, so nothing is
  snapped and a warning says so.
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
