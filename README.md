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
   - **More information needed?** If the drawing review finds a standard, project document
     or piece of information it needs that wasn't supplied (e.g. a table of AS 1684.2, the
     truss layout with girder positions, a BAL report), the review pauses and the page pops
     up a question for each one, with the assumption it would otherwise use:
     - **Yes** - attach the PDF and / or type the information in. Standards are stored with
       the other stored standards (transcribed once, ticked in later reviews); project
       documents are used for this review (and its sheet re-runs). The drawing review then
       runs again with it (one more step-2 call).
     - **No** - carry on with the stated assumption; it is added to the direction, the
       assumptions and the warnings.
     - **Ignore** - carry on without noting it on the sheets (listed in the warnings only).
     With no answer within `INFO_WAIT_MINUTES` (default 30) the review carries on as if
     every answer were No. Closing the pop-up leaves the review waiting; the status card has
     a button to reopen it.
3. **Mark-up drawings.** Produces one PDF with:
   - one marked-up sheet each for load paths, roof (rafters only, including the deck),
     wall framing / racking / blockwork joints, footings (AS 2870) and floor framing or slab.
     Footings and floor are one sheet ("FOOTING & FLOOR PLAN") or, if you
     tick **Footings (own sheet)** / **Floor framing / slab (own sheet)** instead, separate
     FOOTING PLAN and FLOOR FRAMING PLAN sheets (clearer on busy suspended-floor jobs, one
     more sheet call). Ticking one choice unticks the other.

   Sheets follow the house mark-up style: A3, address and sheet name top-left,
   classification and notes down the left, the architectural plan faded in the middle
   with colour-coded member lines and tags (B, J, L, VB, R, C, DS, EB ...), piers,
   columns, bracing panels (`h(b) - 900`, `g - 600`, dashed `d - 2400`), boxed red
   callouts with arrow leaders, the schedule / bracing legend on the right and the title
   block bottom-right if you tick **Add title block** (Job No, Dwg No SSK-x, Rev, Date,
   Made by). Without it the schedule and callouts use that space.

   - a **CALCULATION SHEET** (tick box, on by default; no extra Claude call) at the end:
     - No title block on this page (even when the drawing sheets have one).
     - *Design basis*: site and wind class, structure, governing standard, the standards
       supplied (or a note that values are from the standards as known - confirm).
     - *How the timber bracing is determined* (AS 1684.2 Section 8): wind class; area of
       elevation for each wind direction (Cl 8.3.3 / Fig 8.2, from half the wall height up);
       pressure from Tables 8.1-8.5; racking force = area x pressure; capacity = sum of
       bracing wall length x Table 8.18 capacity for walls parallel to the wind, at least the
       racking force each way; distribution / spacing (Cl 8.3.6) and hold-downs.
     - *Racking forces*: Claude's working for each direction (area parts, pressure and
       table, force).
     - *Bracing check*: the app totals the panels actually drawn on the wall framing plan
       for each direction (type, number, lengths x kN/m) and compares them with the racking
       force - OK, or NOT ADEQUATE with a warning. Re-running the wall sheet updates it.
     - *Member and footing calculations*: basis, working and result for the key decisions
       (roof load widths, lintels / beams, bearers / joists, columns, footing / slab, tie-down).
       Each result is compared with the schedules by tag: where the section differs (e.g. calc
       RB1 2/290x45, schedule RB1 290x35) a CHECK line is added under it and a warning given.
       The sheets are told to use the calculation sizes and never reduce one.

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
| `APP_PASSWORD` | (none, secret) | Shared password for the app (browser asks once; any user name). Unset = open to anyone with the address |
| `JOBS_DIR` | `data/jobs` | Where finished reviews are saved (a persistent disk on Render) |
| `JOBS_KEEP` | `40` | How many saved reviews to keep |
| `OFFICE_PIER_SPACING_MM` | `3000` | Office pier spacing used by the pier spacing check |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model used for every step |
| `ANTHROPIC_EFFORT` | `high` | Reasoning depth for step 1 (geotech review): `low` / `medium` / `high` / `xhigh` / `max` |
| `ANTHROPIC_DESIGN_EFFORT` | `medium` | Reasoning depth for step 2 (architectural review - roof, walls, load paths, footings, bracing). Was `high`; `medium` cuts its thinking (output) cost - set `high` if the assessments get worse |
| `REF_STORE_DIR` | `data/references` | Where stored text extracts of standards are kept (a persistent disk on Render) |
| `REF_PAGE_CAP` | `150` | Most pages of standards / guides sent per review, all documents together (max 500) |
| `REF_MODE` | `digest` | `digest` = read standards from stored extracts; `pdf` = send PDF pages every time |
| `ANTHROPIC_CACHE_TTL` | `1h` | How long the references and drawing pages stay cached between steps (`1h` or `5m`) |
| `ANTHROPIC_JSON_MODE` | `local` | `local` = answer schema in the prompt, JSON checked by the app, so all steps share one cache; `api` = the API's structured outputs |
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
  - shared by every step: the API's structured-output answer format is part of the
    cached prompt, so steps with different answer formats couldn't share a cache (a live
    run showed step 2's cache wasn't reused by the sheets). By default
    (`ANTHROPIC_JSON_MODE=local`) the review steps instead get the answer schema in the
    prompt, after the cached content, and the app validates the JSON (retrying up to 3 times
    if it doesn't match). Step 1 caches the standards, step 2 reads them and caches the
    drawings, and the sheets read both and cache the review results (geotech + design +
    house style) for each other and for re-runs. `ANTHROPIC_JSON_MODE=api` goes back to the
    API's structured outputs, where only the sheets share a cache;
  - effort (high for the geotech review, medium for the drawing review and sheets) is set per message after the
    cached content, so a change of effort between steps doesn't invalidate the cache. If
    the API refuses per-message effort the app falls back to per-request effort by itself.
  The "from cache" share on the results page shows whether caching is working, and a
  **Cache check** under the usage line says, for each hand-over, whether the next step
  reused the cache (shared / not shared, with tokens read and written): geotech review ->
  drawing review (standards), drawing review -> first sheet (standards + drawings), first
  sheet -> other sheets. The same lines are printed to the server log as `[cache] ...: YES/NO`
  and are in review.json under `usage.cache_checks`.

- **Choose the sheets.** "Sheets to produce" on the form has a tick box per sheet, all
  unticked to start; tick the ones you need (at least one) - each sheet left out saves one
  sheet call. Articulation joints are still only made on masonry jobs. A sheet left out can be added
  later from the results with Re-run, without repeating the review.
- **Re-run a single sheet.** Under the results, pick a sheet, optionally say what should
  change ("lintels missing over the laundry window"), and press **Re-run sheet**. Only that
  sheet is redone - the geotech and drawing review are kept, the drawings and standards are
  read from the cache (within the hour), and the PDF is rebuilt with the new sheet. If the
  re-run fails the previous version is kept. Each re-run is listed with its cost and
  counted in the review's usage. Re-runs need the review still in memory (until the
  service restarts).

## Office practice

`app/office_rules.md` holds the office's standard choices, given to Claude in every step
(steps 1-3) unless the engineer's direction for a job says otherwise; the firm rules below
always win. Members are still sized for their span and load - the file sets the form and
material, not a size to copy. Edit the file (one rule per bullet) to change them:

- **Lintels:** single F17 / LVL members (office range L1 90x35, L2 120x35, L3 140x45, L4
  190x45) to the AS 1684.2 span tables, not doubled MGP; continuous lintel / ledger where one
  runs over several openings or carries a carport. Trusses / rafters are taken to span the
  shorter direction of the building, perpendicular to the ridge (checked against the roof
  plan's ridge and fall arrows - sheeting ribs run down the slope - and the overall
  dimensions), unless the drawings show otherwise. Each plan page sent to Claude states the
  building's outline measured from its walls (e.g. "16.3 m across x 9.9 m up the page; the
  shorter direction is up / down the page"). Claude first works out which walls carry
  roof load from the truss / rafter direction - lintels in gable-end and non-loadbearing
  walls are sized without roof load (usually L1 / L2). Office defaults for a single-storey
  sheet roof (roof load width up to ~5 m): loadbearing walls - up to 1200 L1 (L2), up to
  2400 L3 140x45, larger L4 190x45; gable / non-loadbearing walls L1 / L2. Larger only for
  tile roofs, wider roof load widths, girder / point loads, beams over the opening or a
  storey above, with the reason in the notes.
- **Studs / tie-down:** F17 jamb studs > 1500 openings, 2/90x35 F17 DS > 2000; tie-down
  type (a) to lintels, type (b) double strap + M12 for lintels spanning 1800+.
- **Bracing:** a mix of h(b) ply, d strap (internal walls) and g panels, distributed through
  the building in both directions.
- **Footings:** strips 450 x min 450 with 4/L11 TM top and bottom; internal beams with
  3/L11 TM; 450 bored piers at corners, steps and joints and 3000 centres (closer only if needed, with the reason), 1/N16; 230x230 brick piers
  (350x350 over 1500 high); carport slabs 100 thick SL82 with edge / internal beams.
- **Floor and decks:** F7 TP bearers and joists; **decks timber, including ledgers, unless
  the engineer's direction says otherwise** - a steel system named on the architectural
  drawings is listed as an engineer check, not used; 89x3.5 / 89x5 SHS columns on
  plates and bored piers.
- **Site class:** the class the geotechnical report gives - a low-bearing band is handled by
  founding below it, not by re-classifying as P unless the report says P.

- **Member sizes - office stock list:** timber F17/LVL, F7 TP and MGP10 sections, SHS / CHS /
  RHS / PFC / UB, masonry angles and flat bar, and Lysaght Z/C 15015 purlins (listed in
  `app/office_rules.md`). Claude uses these first for every member wherever one works; any
  schedule size not on the list is named in the warnings ("size(s) not on the office stock
  list"). Edit the list in the file and the check follows it.
- **Lintel extras:** entry doors / openings up to 1200 L1 90x35; a continuous lintel / ledger
  carrying a carport or verandah L4 190x45; trusses cantilevering over a recessed wall keep the
  normal lintel with "TRUSS MANUFACTURER TO CONFIRM ABILITY FOR TRUSSES TO CANTILEVER".
- **Masonry lintels** (brick veneer / masonry skin): steel by span - to 900 75x10 FL, 900-1500
  100x75x8 UA, 1500-3300 150x100x10 UA - min 150 bearing. On brick veneer / masonry jobs the
  wall framing sheet gets a **MASONRY LINTEL SCHEDULE** section in its schedule box (span /
  steel size and the bearing note).
- **Slab mesh:** house, paving / porch and carport slabs SL82 top with 30 cover (not SL72).
- **Perimeter footing under a slab** (strip footing or edge beam): 450 x min 450, 4/L11TM top and
  bottom - never 3/L11TM bottom only (checked: a warning names any that isn't). Internal beams
  min 400 deep, 3/L11TM bottom. Concrete N25 (checked: anything below N25 is named).
- **External slabs separate:** porch, paving, patio, alfresco and carport slabs are separate
  from the building slab, isolated with a 10 Abelflex expansion joint and mastic seal (also
  round posts / piers through them); porch / paving slabs 100 thick SL82 with saw cuts at max
  1500 and no edge beam unless a load needs one. The app adds the expansion joint note and
  warns when an external slab is drawn wrapped in edge beams or scheduled with one.
- **Carport / garage slabs:** no internal beam up to 8 m long both ways; internal beams only
  where a dimension is over 8 m or a load needs one, with the reason.

**Checked afterwards** (`app/office_checks.py`, warnings only - nothing is changed): piers
drawn more than the office 3000 mm apart along a bearer / footing line; deck framing in
steel; lintels that aren't single F17 / LVL members; more than one deck joist type; all
bracing panels of one type; plus sizes not on the stock list.

The rest are instructions to Claude (not enforced afterwards like the firm rules), so check
the mark-ups as usual.

## Design rules

Firm rules are given to Claude **and** enforced on its output (`app/rules.py`), so they
hold even if Claude slips:

- **Lintels and bracing are within the building.** The building's outline is found from
  its walls in the drawing (`app/footprint.py`: the connected wall network with wide door
  openings closed; roof sheeting, eaves hatching, decking and stair treads ignored). A
  lintel or bracing panel outside it - on a deck, verandah, carport or eaves - is moved onto
  the nearest building wall alongside it (within about 25 pt) and re-snapped there; one with
  no building wall nearby is removed. Both are listed in the warnings. Vector drawings only.
  If more than half of the lintels and bracing fall outside the outline found, the outline is
  taken to be wrong (typically an alteration, where the new work is drawn lighter than the
  existing house) and nothing is moved or removed - a warning says to check them instead.
- **Racking note on the wall sheet** (with the calculation sheet on): Claude's own racking /
  bracing totals note is replaced by one from the app's bracing check of the panels as
  drawn - required and provided kN each way, OK or NOT ADEQUATE, "REFER CALCULATION SHEET" -
  so the notes and the calculation sheet always agree.
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
  **Upload a standard once.** Every standard you upload is kept (its PDF as well as the
  text) and listed under **Stored standards** on the form; tick one, with its own
  "Pages to review", to use it in a review without uploading it again. Standards stored
  before this feature show without a tick box until they are uploaded once more.
  **Forget** deletes the PDF and its text.
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
- **Footings are drawn as bands, as the engineers draw them**: two lines at the footing
  width (to scale). Strip footings (SF) have both lines dashed; edge beams (EB) have a solid
  line on the outside face and a dashed line on the inside face (worked out from the whole
  footing outline, so recesses come out right), with every corner closed. Internal beams are two dashed lines stopping at the inside face of the perimeter
  footing. Footing runs Claude leaves untagged are tagged from their position (perimeter -
  EB / SF, inside - IB, with a warning), and a slab is shown by one diagonal line (S1)
  corner to corner, as the engineers draw it - a cloud drawn round the slab is replaced.
- **Slab callout and re-entrant bars**: the slab line has an arrow at each end and the slab tag
  only (S1, S2 - the schedule gives the detail) at the middle of the line in a tight circle
  with a white background. At every re-entrant (inside) corner of the slab outline the app
  draws 3 bars across the corner at 45 degrees (3/N12 x 2000 long, 100 apart, first 40 from
  the corner) and adds the re-entrant bar note. A porch / paving strip butting into the
  outline is ignored when finding the corners.
- **Callouts don't overlap**: callout boxes go in one row along the top, one column down the
  right and one row along the bottom, each in the order of its anchors, with the leader
  leaving the box on the face towards the plan. Boxes narrow (and grow taller) to fit a row;
  neighbours whose leaders would cross swap places, and a callout moves to another band if
  that clears a crossing. Anything that still doesn't fit goes to the notes. Tags are placed
  after the callouts, so they keep clear of the boxes and leaders too.
- **Wet areas on an existing suspended slab**: where the design has a shower / bathroom on an
  existing Bondek / concrete upper floor, the floor (or footing & floor) sheet gets an
  engineer-check note for the set-down or screed the falls need and the slab's capacity -
  unless a note or callout already covers a set-down / screed.
- **Alterations over more than one floor**: the wall framing sheet marks up the plan with most of
  the new wall work and labels each item on another floor with its floor (office rule).
- **Driveways are ignored**: not part of the structural mark-up. Driveway slabs, their
  schedule items, callouts and notes are removed with a warning (a note that the driveway is
  'by others' stays).
- **Typical tags, not every one**: pier tags (P1, P2 ...) and footing run tags (SF, EB, IB)
  are shown once per stretch - a tag is dropped where the same tag is already shown within
  about 3.5 m (`TYPICAL_TAG_MM`); the schedule and the 'TYPICAL' callouts cover the rest.
  Bearers, joists, lintels, columns and bracing keep every tag.
- **Ledgers are LD, deck bearers keep the deck tag**: ledgers (deck, roof, to an existing
  wall) are tagged LD, never L (lintels only) - a ledger Claude schedules under an L tag is
  re-tagged LD with the same number where free (L5 -> LD5) on the members, schedule, callouts
  and notes, with a warning. Bearers under a deck / ramp / landing, including the one at the
  deck's outer edge, take the deck bearer tag (office rule).
- **Units in their SI case, nothing 'from memory'**: the sheets are upper case, but units keep
  their case (450 mm, 3.4 m, 36.3 m², 27 kN, 6.0 kN/m, 0.8 kPa, 25 MPa; dimensions 90x35) -
  Claude is told so and the app corrects any that slip through (only after a number, so M12
  bolts, N25 concrete and L11 bars are untouched). Notes, callouts, schedules and the
  calculation sheet never say a value is 'from memory' or a table 'not supplied': they cite
  the standard and write ENGINEER TO CONFIRM where a value needs checking. Your own sheet
  notes are added word for word.
- **British / Australian English only**: Claude is told to use it, and common American
  spellings on the sheets (centers, galvanized, vapor, color, curb ...) are corrected
  (`app/spelling.py`).
- **Tags (P1, EB1, L3 ...)** are 9 pt (`MARKUP_TAG_FONT_SIZE`) in tight boxes with a white
  background, placed after everything else is drawn in the first nearby spot that overlaps no
  other tag, member line, arrow, pier, column or cloud. A tag moved away from what it names gets
  a short leader line; a repeated tag with no clear spot near one already shown is left out.
- **Mark-ups are drawn roughly to scale.** The plan scale is taken, in this order, from:
  the scale printed on the sheet (a scale for other paper is converted - '1:100 @ A1' on an
  A3 page is 1:200); the scale Claude reads off the page in the drawing review (CAD text is
  often line work the app can't read); a scale printed elsewhere in the set; or, last,
  inferred from the 90 mm stud walls. Using it, piers are drawn at their diameter from the schedule (450 dia by
  default), brick / block piers as a square of their size inside the bored pier, and columns
  at their section size (e.g. 89 SHS) - kept just large enough to see. Bracing panels are
  drawn at their tagged length and lintels / beams / footings / joists are snapped to the
  real walls, openings and supports (see below). Member line weights stay in house style.
  Each drawing page sent to Claude states its scale ("1000 mm on site = 0.0238 of the image
  width"), and Claude is asked to draw every bearer line at its scheduled spacing and every
  rafter (deck / verandah / carport roofs) at its spacing, with spacings in the schedule
  ("AT 2200 CTRS"). Bearers or rafters drawn further apart than their scheduled spacing
  (by more than 25 %), or a rafter group with a spacing but only one rafter drawn, are named
  in the warnings.
- **Mark-ups are never silently left off a sheet.** If Claude puts members outside the plan
  area it chose for the sheet, the plan area is enlarged to show them (with a warning to
  check they are on the right drawing); anything still outside, or a sheet that came back
  with no members at all, is named in the warnings. A sheet whose Claude call fails is
  retried once before it is reported as not produced, and any Claude call that is refused
  because the API is busy (rate limit 429, overloaded 529, server error) waits 30 s / 60 s /
  120 s and tries again.
- Mark-up positions come from Claude reading the page image, so they are approximate;
  check them and drag them into place in your PDF editor.
- **Footings, bearers and beams are snapped to the walls** in a CAD-exported drawing:
  edge beams / strip footings / internal beams (EB, SF, IB) go on the wall centreline, and
  bearers / beams / ledgers (B, VB, RB, LD) too, or about 3 pt beside a footing line
  already on that wall (house style, so both lines show). A member only snaps when a wall
  runs along at least half its length, so bearers spanning mid-room and deck beams stay
  as placed. Ends snap to the crossing walls, so footing corners close. Footing lines
  that are not on any wall are named in the warnings.
- **Bearers are snapped to their footings and piers** on the floor (or footing & floor) sheet: a bearer
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
- **Joist and rafter arrows run across the members** (house style): each joist / rafter group
  is one representative member drawn along its span plus a double arrow drawn
  PERPENDICULAR to it, across the area where it repeats at its spacing. An arrow Claude
  draws along the span is turned 90 degrees about its middle (each arrow is checked against
  the nearest member with its tag, so two decks both tagged J2 can span different ways), and
  every representative joist / rafter without its own arrow gets one, across its middle. Truss arrows (T) still show
  the assumed truss span direction. Span arrowheads are small (3 pt, about a quarter of
  the old size) and each arrow is one editable polyline (1 pt line).
- **Floor joists span wall to wall.** The representative joist is stretched / trimmed to run
  from external wall to external wall, continuous over the bearers, and the joist arrow is
  trimmed so it ends at the walls (never past them). The external walls are the sheet's
  perimeter footing lines (SF / EB) where drawn - they follow recesses and decks - else the
  building outline. Deck joists (outside the building) are left as drawn.
- **Schedules list only the member type and spacing.** Notes, locations, spans, roof load
  widths and fixings are cut from schedule and legend items (they belong in callouts /
  notes); footing and slab reinforcement and any "AT ... CTRS" spacing are kept.
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
- **Continuous lintels / ledgers** (schedule says CONTINUOUS or LEDGER) keep their length and
  are only moved onto their wall - they are not cut down to one opening.
- **Lintels are snapped to the openings** when the architectural PDF is a CAD export
  (vector line work): each lintel is moved onto the wall centreline, from jamb to jamb
  plus about one wall thickness of bearing each side (`app/snap.py`). Where no clear
  opening is found, or two openings fit equally well, the lintel is left as Claude
  placed it and named in the warnings. Scanned drawings have no vectors, so nothing is
  snapped and a warning says so.
- **Finished reviews are saved** (`app/jobstore.py`, on the Render disk): the results page,
  review.json and mark-up PDF of the newest 40 reviews survive restarts and redeploys and are
  listed under **Previous reviews**. A saved review can be opened and downloaded; re-running a
  sheet needs the review still in memory (the last 8 since the service started), because the
  drawings themselves are not stored.

## Password

Set `APP_PASSWORD` in the Render service's **Environment** tab. The browser then asks for a
user name (anything) and that password once per session; without it the page, the API and the
stored standards are open to anyone who has the address. `/healthz` stays open for Render.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

The tests need no API key and no client drawings (Claude is never called): firm rules,
schedule trimming, stock list, sheet selection, arrows, office checks, cache checks, the
password, saved reviews, stored standards and PDF rebuilds.

## Code layout

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI routes and the background job runner |
| `app/pipeline.py` | Runs steps 1 to 3 in order |
| `app/claude.py` | Prompts and Claude API calls (structured outputs) |
| `app/schemas.py` | Output schema for each step |
| `app/pdf.py` | Renders pages and draws the mark-up PDF |
| `app/static/index.html` | Upload page |
