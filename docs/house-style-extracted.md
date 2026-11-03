# House style and preferences extracted from past jobs

Source sets: 2024-C07-PR01 (65 Bay Rd, 3 units, class M, N2), 2024-C07-PR03 (19 Schouten
St, 2 units, class M, N2), 2024-C07-PR05 (5 Bridgeview Rd, two-storey dwelling + garage +
shed, class P, N3).

Everything below is now data in `app/data/house/` and will be editable in the app under
**Settings → Preferences / Notes / Details**. Items marked **[?]** need your confirmation.

## Sheet set and layout

- Mark-up sheets per building / unit: **Footing (and Floor Framing) Plan**, **Wall Framing
  Plan**, **Roof Framing Plan**. Two-storey: Footing & Slab, Wall Framing GF, Floor Framing
  FF (& Deck), Wall Framing FF (or Roof/Wall FF combined), Roof Framing. Garage / shed get
  their own Footing & Slab and Roof/Wall sheets.
- The current tool's separate "Load Path Plan" has no equivalent in your sets. **[?]** Keep
  it as an optional internal sheet, or drop it?
- Plan at 1:100 @ A3; schedules and notes stacked in the right-hand column (older set:
  bottom-left). Classification banner at the top of the column:
  `AS 2870:2011 SITE CLASSIFICATION: M` on footing/floor sheets,
  `AS 4055:2021 WIND CLASSIFICATION: N2` on wall/roof sheets.
  **[?]** The 2026 set says "AS 4055:2021 SITE CLASSIFICATION" – I've assumed WIND.
- Schedule boxes, in order: COLUMN SCHEDULE, FOOTING SCHEDULE, FLOOR FRAMING SCHEDULE /
  LINTEL SCHEDULE, MASONRY LINTEL SCHEDULE, BRACING SCHEDULE, ROOF FRAMING SCHEDULE, NOTES,
  NOTICE TO OWNER (footing sheets only).
- Truss roofs: `TR1` box at centre and one diagonal double-headed arrow corner to corner,
  schedule "ROOF TRUSSES BY MANUFACTURER" / "TRUSSES TO MANUFACTURERS SPECIFICATIONS".
- Load-bearing internal walls labelled `LBW`; "LBW BELOW" on upper floor plans.

## Tag conventions

| Prefix | Meaning | Typical |
|---|---|---|
| C | SHS column | C1 89×3.5 SHS (standard), C2 89×5 SHS (continuous / heavier) |
| DS | Double stud | 2/90×45 F17/LVL (2/90×35 in one job) |
| P | Bored pier Ø450 | P1 to refusal/rock with base plate for SHS; P2 at corners & max 3000 ctrs under strip footing, 1/N16 central (or with 230×230 brick pier above); P3 with 600 taper at top under slab |
| SF | Strip footing | SF1 450 W × min 450 D, 4/L11TM T&B, 50 cover (500 D on class P) |
| EB / IB | Edge beam / internal beam (slab) | EB1 450 W × 450 D 4/L11TM T&B + 1/N16 top; IB1 300 W × 400 D 3/L11TM bottom |
| SL / "150 RC" | Slab | House 100 thk SL82 top 30 cover 25 MPa; garage 150 thk SL92 T&B 30 cover 32 MPa |
| RE | Re-entrant bars | 3/N12 2000 long, 100 ctrs, first bar 40 from corner |
| B | Bearer | 2/140×45 F17/LVL (internal) · 2/290×45 F7 TP or 2/140×45 F17/LVL equiv. (external → TP) · steel 250UB37 / 310UB40 |
| J | Floor joist | 140×45 or 190×45 F17/LVL at 450 ctrs; 300 ctrs under tiled areas |
| DB / DJ / DL | Deck bearer / joist / ledger | 2/140×45 or 2/190×45 F7 TP; 90×45 or 140×45 F7 TP at 450; ledger 2/140×45 F17 TP |
| L | Lintel | F17/LVL, numbered by size within the job: 90×35, 120×35, 140×35, 190×35, 190×45, 240×35, 240×45, 2/290×45; steel 250 PFC |
| VB | Verandah beam | 190×45, 2/190×45, 2/290×45 F7 TP; or 140×45 / 240×45 F17/LVL; annotated "rated for max 2000 RLW" |
| RB | Roof beam | 250 PFC |
| BP | Base plate | 100×230×12, 190×90×10, 190×190×10, 250×250×12 |
| RW | Retaining wall | RW1 "see detail" – out of scope for the engine |
| d / g / h(B) | Bracing type | d = double diagonal strap (1800, 2400); g = ply 600; h(B) = ply method B (900, 1600, 1800, 2800, 3600) |

Bracing labels: `h(B) - 900`, `d - 2400`, `g - 600`.

## Default member selection order

1. Timber to AS 1684 span tables, grade F17/LVL internally, F7 TP externally.
2. Doubled timber members before going to steel.
3. Steel (PFC lintel / UB bearer / SHS column) when outside the tables or span > ~3.3 m
   over openings. **[?]** confirm the trigger.
4. Trusses never detailed – "by manufacturer"; truss manufacturer to confirm lintel
   capacity for concentrated loads.

## Standard callouts (parameterised in the library)

- `6 CFW 8 PLATE CLEAT TO {col}. CLEAT CONNECT {beam} TO {col} WITH {n}/M{d} 4.6/S BOLTS.
  MIN 50 BETWEEN BOLTS AND TIMBER END ...`
  Bolt rule: 140 deep → 2/M10, 190 deep → 3/M10, 240 deep → 3/M12.
- `Ø450 BORED PIER AT CORNERS AND MAX 3000¢ UNDER SF1 (TYPICAL)`
- `LOCALLY WIDEN STRIP FOOTING BY 250 W × 400 L TO SUPPORT COLUMN AND BASE PLATE`
- `110 FOUNDATION BRICKWORK WITH ENGAGED PIERS AT MAX 1800¢ TO MAX 1500 HIGH. WHERE HEIGHT
  GREATER THAN 1500 PROVIDE DOUBLE BRICK TO MAX 2400 TOTAL HEIGHT (TYPICAL)`
- `BRICK VENEER NOT TO RETAIN MORE THAN 100mm FILL. WHERE GREATER THAN 100mm BUT LESS THAN
  1000mm ... REPLACE WITH 190 COREFILLED BLOCKWORK WALL N12 AT 400 EW COGGED INTO STRIP FOOTING`
- `190 CORE FILLED BLOCKWORK WALL – MAX RETAINING HEIGHT 1 m. N12 AT 400¢ EACH WAY – N12
  COGGED STARTER BARS TO SF1`
- `REDUCE JOIST SPACING TO 300¢ UNDER TILED AREAS` / `JOISTS TO BE CONTINUOUS AND SPACED AT
  300¢ UNDER TILED AREAS`
- `BOLT DECK LEDGER THROUGH TO FLOOR FRAMING WITH M12 4.6/S BOLTS TO EACH END AND AT MAX 300¢`
- `DJ1 MAY BEAR ON DB1/DL1 OR BE HANGER CONNECTED. HANGERS TO BE STAINLESS STEEL OR
  OTHERWISE RATED FOR EXTERNAL USE`
- `ISOLATE BUILDING FOOTINGS FROM ALL PAVING SLABS WITH 10 ABELFLEX JOINT AND MASTIC SEAL`
- `PROVIDE N12 TIE BARS BETWEEN SF1 AT 400¢ WHEN SF1 AND 150 RC SLAB POURED SEPARATELY`
- `VERANDAH BEAMS RATED FOR MAX {rlw} ROOF LOAD WIDTH. TRUSS MANUFACTURER TO CONFIRM
  SUITABILITY OF FRAMING FOR ANY POINT LOADS`
- `C2 TO BE CONTINUOUS FULL HEIGHT COLUMNS TO SUPPORT PFC LINTEL ABOVE ...`
- `BOLT WALL FRAMING TO C2 CONTINUOUS COLUMNS WITH M10 BOLTS AT MAX 600¢`
- `{n} STITCH WELD 150×100×10 UA TO L6 HIT 100 MISS 100`

## Plan notes (verbatim, now in `notes_library.yaml`)

Footing / floor sheets: member-layout-only disclaimer; steel corrosion protection; site
preparation to AS 2870 / AS 3798; step footings AS 2870 5.4.3; sub-floor ventilation,
flashing and DPC to AS 4773; articulate masonry to AS 4773; ground falls 2 % / 5 %; AG
drains before footing excavation; trench bases fall 1 %; bolted connections to AS 1720.1
and AS 4100; bearer cleat bolt rule; Notice to Owner (CSIRO BTF 18).

Wall / roof sheets: trusses to manufacturer; truss manufacturer to confirm lintel capacity;
internal walls non-load-bearing UNO; F17 studs to sides of openings > 1500, 2/90×35 F17/LVL
> 2000; studs > 2600 high F17; typical tie-down Type A (30×0.8 strap, 6/30×2.8 nails, M10
to floor) and Type B (double strap + M12) for lintels ≥ 1800 on the top floor; bracing to
AS 1684.2.

## General-notes sheets

Captured as a block library: General, Concrete (+ Table 1 cover), Reinforcement
(+ Tables 2–3 laps), Retaining walls, Structural steel, Unreinforced masonry, Timber
framing, Table 4 tie-down (N2 and N3 variants), Inspections & hold points, Strip footings,
Slabs/edge beams/rafts, Earthworks (+ Tables 5–6), Unprotected embankments, WHS sheets 1–2.
These aren't produced by the mark-up tool (they're CAD sheets) but the notes library
references the same wording so the mark-up and the final set agree.

## Standard details seen (for the details library)

C1 / C2 base plate · garage column BP · BP1 · slab base · slab on fill · strip footing
option EB1 · raft option EB1 · internal beam class M masonry veneer · SF1 · P3 tapered pier
· RW1 and retaining wall type 2 (0–1800) with reinforcement legend · bracing type d ·
bracing type h method B · tie-down framing detail · bracing wall to external wall (3 variants)
· truss & wall fixing · tie-down truss/rafter to stud wall · tie-down stud wall to slab ·
C1 connection detail · footing detail / alternate footing detail (Schouten).

These will be stored as image/PDF snippets with a key, inputs they depend on (site class,
construction type, member size) and a flag `source: hand_drawn | generated`. Generated
ones (Claude) are placeholders you replace by uploading your own.

## Fourth set: Lot 2 Rada Rd, Kettering (2024-C19-PR01) – designer-drawn, Arete-certified

Different workflow: the building designer (Another Perspective) drew the structural
sheets (04 Structural Slab lower/upper, 05 Section, 06/06a/06b details, 07 Bracing / Lintel
Plan, 08 Bracing & tie-down details) and Arete certified them – the "STR CERT" PDF is the
same file with the Arete stamp added to those pages. So this one is **not** an Arete
design and, per your instruction, nothing from it goes into the defaults. It is still
useful in three ways:

1. **Input→output test case.** It's the only pair where we have the architectural PDF.
   Class S, N3, BAL 29, truss roof 15° sheet, FC-clad timber frame on 20.01 core-filled
   block subfloor, 160 Bondek suspended slab on 250UB25.7 beams and 89×3.5 SHS posts to
   Ø450 piers on rock; garage on 100 SOG. The floor plan is vector with 162 dimension
   strings, scale `1 : 100 @ A2`, and site class / wind class / BAL in the title block text.
2. **A "check" mode.** The tool's engines can run on a designer's structural sheets and
   report differences against the deterministic result – which is exactly what
   certification is. Worth a later phase.
3. **Things that differ from Arete practice** – flagged, not copied:
   - Strip footings 450 W × **400 D with 4/L8 TM** T&B (Arete: 450 D, 4/L11 TM). Founded
     on rock, so may be fine, but the engine will size from AS 2870, not from here.
   - Lintels 2/L3 (2/190×35) and 2/L4 over wide openings instead of a deeper single;
     L6 = **200** PFC on 75×4 SHS (Arete: 250 PFC on 89×5).
   - Jamb-stud rule in F7 TP: 2/90×35 < 1800, 2/90×45 1800–3200, 3/90×45 3200–4000
     (Arete: F17, > 1500 and > 2000). Both are AS 1684 Table 6.x lookups – the engine will
     use the table, which settles it.
   - "h(B) – wall" labels (whole wall braced, no length) and bracing capacities quoted on
     the sheet: type h method A 5.6 kN/m, method B 5.2, type d 3.0, type c 1.5, with
     "values for bracing < 900 not included". I'll take these from Table 8.18 when you
     send it, not from this drawing.
   - A stud tie-down table (MGP10 / MGP12 / F17 × size × RLW × strap spacing) that looks
     supplier-derived. If you'd like it in the library, tell me its source.

## Open points for you

1. Load Path sheet – keep or drop?
2. Banner wording for wind: "WIND CLASSIFICATION"?
3. Steel trigger (span / load) and whether you want option A (matrix) or B (AS 4100 engine).
4. Do you ever use waffle rafts, tile roofs, or MGP grades? If not I leave those branches out.
5. Tie-down: encode your Table 4 variants (recommended) – send N1 / N4 / tile / lower-storey
   versions if you have them.
6. Test fixtures: the architectural + certified pair for Rada Rd is ~21 MB each. I'd keep
   fixtures out of plain git (LFS or a gitignored `fixtures/` folder you sync separately).
   Ideally I'd also get the **architectural PDFs** for Bay Rd, Schouten St and Bridgeview Rd
   so all four become regression tests.
