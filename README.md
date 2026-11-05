# Resi Mark-Up Tool

Web app that uses the Claude API to prepare **preliminary** structural mark-ups for
residential projects. All output is a design aid for the engineer and must be
checked before use. It is not for construction.

## What it does

1. **Geotechnical review and wind.** Reads the geotechnical documents (report, addenda, logs - PDF) and records the
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
   - **Additional notes & direction** (optional, bottom of the form):
     - *Direction for the review*: anything the drawings don't show, or how you want it
       done (e.g. "use LVL for all lintels", "stiffened raft, no waffle pod"). Claude follows
       it in the geotech review, the drawing review and on every sheet (in the geotech
       review it still records what the report says and flags any difference); it overrides defaults but not the firm
       rules below.
     - *Additional notes for the drawings*: one note per line, added word for word (in
       capitals) to the notes column of every sheet, after Claude's own notes. A note Claude
       already wrote isn't repeated, and the firm rules never rewrite these notes.
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
| `REF_STORE_DIR` | `data/references` | Where stored text extracts of standards are kept (a persistent disk on Render) |
| `REF_PAGE_CAP` | `150` | Most pages of standards / guides sent per review, all documents together (max 500) |
| `REF_MODE` | `digest` | `digest` = read standards from stored extracts; `pdf` = send PDF pages every time |
| `ANTHROPIC_CACHE_TTL` | `1h` | How long the references and drawing pages stay cached between steps (`1h` or `5m`) |
| `ANTHROPIC_PER_MESSAGE_EFFORT` | `1` | Set effort per message so all steps share one cache; `0` = per request |
| `ANTHROPIC_SHEET_EFFORT` | `medium` | Reasoning depth for step 3 (placing the mark-ups on each sheet) |
| `ANTHROPIC_TIMEOUT_S` | `1800` | Max seconds a Claude stream may sit silent before it is retried once |
| `ANTHROPIC_MAX_TOKENS` | `64000` | Output cap per Claude call (thinking counts toward it); a cut-off call retries once at 128000 |
| `LINTEL_MAX_SPAN_DEPTH` | `20` | Lintel check: flag a lintel whose clear span exceeds this many times its depth |
| `BEAM_WIDTH_FROM_MM` / `BEAM_WIDTH_TO_MM` | `300` / `450` | Edge / internal beam width rule |
| `MIN_STRIP_FOOTING_WIDTH_MM` | `450` | Firm minimum strip footing width (see Design rules) |
| `TITLE_BLOCK_COMPANY` | `ARETE` | Company name in the title block |
| `TITLE_BLOCK_COMPANY_SUB` | `ENGINEERING` | Second line under the company name |
| `MARKUP_AUTHOR` | `Resi Mark-Up Tool` | Author shown on every annotation in Bluebeam's markup list |

## Cost

- **Usage and cost of every review.** Every Claude call's tokens (uncached input, cache
  writes, cache reads, output including thinking) and an estimated US$ cost are recorded
  (`app/usage.py`). The results page shows the total ("Claude usage: 9 calls - 360k input
  tokens (89% from cache) - 64k output - est. US$1.50") with a per-step table; review.json
  has the same under `usage`, and each call is printed to the server log as `[usage] ...`.
  Prices are list prices in `app/usage.py` - an estimate; the Claude Console has the bill.
- **Caching.** The references and drawing pages are sent first in every step and cached:
  - for 1 hour (`ANTHROPIC_CACHE_TTL`), not the default 5 minutes, because steps can be
    more than 5 minutes apart and a lapsed cache means paying to send everything again;
  - only by the sheets: a live run showed the sheets can't reuse the drawing review's
    cache (each step's answer format is part of the cached prompt), so the geotech and
    drawing reviews no longer pay to write one. The sheets cache the standards, drawings
    and the review results (geotech + design assessment + house style) for each other, so
    the second sheet onwards (and re-runs) read them at the cache price;
  - effort (high for the reviews, medium for the sheets) is set per message after the
    cached content, so a change of effort between steps doesn't invalidate the cache. If
    the API refuses per-message effort the app falls back to per-request effort by itself.
  The "from cache" share on the results page shows whether caching is working, and a
  **Cache check** under the usage line says, for each hand-over, whether the next step
  reused the cache (shared / not shared, with tokens read and written): geotech review ->
  drawing review (standards), drawing review -> first sheet (standards + drawings), first
  sheet -> other sheets. The same lines are printed to the server log as `[cache] ...: YES/NO`
  and are in review.json under `usage.cache_checks`.

- **Choose the sheets.** "Sheets to produce" on the form has a tick box per sheet (all
  ticked by default; articulation joints are still only made on masonry jobs). Untick any
  you don't need - each sheet left out saves one sheet call. A sheet left out can be added
  later from the results with Re-run, without repeating the review.
- **Re-run a single sheet.** Under the results, pick a sheet, optionally say what should
  change ("lintels missing over the laundry window"), and press **Re-run sheet**. Only that
  sheet is redone - the geotech and drawing review are kept, the drawings and standards are
  read from the cache (within the hour), and the PDF is rebuilt with the new sheet. If the
  re-run fails the previous version is kept. Each re-run is listed with its cost and
  counted in the review's usage. Re-runs need the review still in memory (until the
  service restarts).

## Design rules

Firm rules are given to Claude **and** enforced on its output (`app/rules.py`), so they
hold even if Claude slips:

- **Lintels and bracing are within the building.** The building's outline is found from
  its walls in the drawing (`app/footprint.py`: the connected wall network with wide door
  openings closed; roof sheeting, eaves hatching, decking and stair treads ignored). A
  lintel or bracing panel outside it - on a deck, verandah, carport or eaves - is moved onto
  the nearest building wall alongside it (within about 25 pt) and re-snapped there; one with
  no building wall nearby is removed. Both are listed in the warnings. Vector drawings only.
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

- **Several drawing sets and geotechnical documents.** Architectural drawings and
  geotechnical documents are added the same way as standards: **Add** appends (or drag PDFs
  in), each file has **Remove** and its own **Pages to review** (blank = all).
  - Drawing sets (e.g. plans, elevations, revised sheets) are combined in upload order into
    one set for the review and the mark-up; Claude is told which file and page each drawing
    page came from. Up to 60 pages in total (`MAX_DRAWING_PAGES`), each file under 30 MB.
    Use the pages box (e.g. `3-12, 15`) to skip covers, 3D renders and BASIX pages: fewer
    pages is faster and cheaper. Large sets are sent at a lower image resolution so they
    fit in one request.
  - Geotechnical documents (report, addenda, borehole logs) are all reviewed together in
    step 1; up to 22 MB in total (they are sent inline in one request), so use the pages box
    to skip long appendices.
- **Standards are read once, then reused** (`app/refstore.py`). The first time a page of a
  standard / guide is needed, Claude transcribes it to text (clauses word for word, tables
  row by row, notes, what each figure shows) and the text is stored, keyed by the PDF's
  content (so renaming the file doesn't matter). Every step of the review, and every later
  review that needs that page, sends the stored text instead of the PDF page - only pages
  never read before are extracted. The results page shows, per document, how many pages
  came from the store and how many were newly extracted. **Stored extracts** under the
  standards box lists what's stored, lets you view the text (check a span table before
  relying on it) and **Forget** a document so it is read from the PDF again. A page that
  can't be transcribed is sent as a PDF page and retried next time.
  The store is on the Render persistent disk (`REF_STORE_DIR`, 1 GB in `render.yaml`);
  `REF_MODE=pdf` turns it off. `REF_EXTRACT_EFFORT` (default `low`) sets the transcription
  reasoning depth.
- Reference documents (standards, guides, codes) can be up to 100 MB each. Leave a
  document's **Pages to review** blank and the app reads its bookmarks, contents pages and
  the first lines of every page, asks Claude which sections this job needs (footings,
  bracing, tie-down, span tables ...), and sends only those pages (`app/refpages.py`).
  The results page lists, for every document, the pages used, the sections they cover and
  why. Type a range (e.g. `45-60, 72`) to choose yourself, or `all` for the whole document.
  Documents of 25 pages or less are sent whole. If a long document's contents can't be
  read (a scan) or no relevant section is found, it is not sent and a warning asks for its
  pages. The pages sent across all documents total at most 150 per review (`REF_PAGE_CAP`,
  set low for beta to control cost; up to 500). When several documents are picked
  automatically the cap is shared between them, and a typed range that goes over it is
  refused with a message.
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
- **Articulation joints are on their own sheet, drawn on the elevations**
  ("ARTICULATION JOINT ELEVATIONS", masonry / brick veneer / blockwork jobs only). Claude
  draws each AJ / CJ as a vertical line on the elevation page; window / door openings are
  found in the elevation line work (`app/elevation.py`) and any joint that runs through
  one is moved to its jamb and made vertical, with a warning. Joints are removed from the
  plan sheets.
- **Plan view dimensions are checked against the elevations**: Claude matches each
  elevation to its wall and compares the PROPOSED plan dimensions with the PROPOSED
  elevations - overall lengths and depths, wall lengths and steps, wall / ceiling heights
  and floor levels, eaves overhangs, and the extent of every closed / covered roof area
  (alfresco, porch, verandah, carport, deck roof). Each comparison (plan value, elevation
  value, match / mismatch / not shown) is shown in a table on the results page and in
  review.json; every mismatch is also a warning. To help, each page sent to Claude lists
  its written dimensions and levels with their positions.
- **Plans and elevations are cross-referenced for openings**: Claude matches each
  elevation to its wall and checks every opening's code, size and position, listing any
  discrepancy as a warning; a secondary line-work comparison of opening widths (plan codes
  vs elevation frames) is also reported for checking.
- **Lintels use the architect's opening codes**: the code picks which opening a lintel
  belongs to, then the opening's jamb lines give its exact edges (codes alone if the jambs
  can't be read). Brick veneer walls (several face lines) are recognised as walls; only
  decking / stair treads (7+ identical parallels) are excluded.
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
