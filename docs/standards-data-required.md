# Standards data required for the deterministic design engines

This is the list of tables and clauses the rebuilt tool needs so that **every routine
design decision is a lookup or calculation, not an LLM call**. Nothing here is
shipped from memory: the values come from Arete's licensed copies of the standards.

## How to supply them

- A **screenshot / photo of the table page** is the easiest. I transcribe it into the
  data format below and you check the file (one table = one file, so checking is quick).
- Typed values or a spreadsheet are equally fine.
- If a table number below doesn't match your edition, use the title / purpose column
  to find it. Table numbers are from memory and are marked *(confirm)* where I'm not
  certain of the numbering.
- Rows you never use (e.g. tile roof, C-region wind classes) can be left out; the engine
  then reports "outside tabulated range – engineer to design" instead of guessing.

## Storage format

Every table lives in `app/data/standards/<standard>/<table>.yaml` (small matrices) or
`.csv` + `.yaml` sidecar (big span tables). The YAML carries the provenance that the
per-job trace quotes:

```yaml
standard: AS 1684.2
edition: 2021
table: "8.18"
title: Structural bracing – wall types, capacities and minimum lengths
inputs: [bracing_type, method]
outputs: [capacity_kn_per_m, min_length_mm]
units: {capacity_kn_per_m: kN/m, min_length_mm: mm}
source_checked_by: ""      # initials + date once you have checked the values
rows: []
```

Editions assumed: AS 4055:2021, AS 2870:2011, AS 1684.2:2021 (+ Supplements),
AS 4773.1/.2:2015, NCC 2022 Volume Two + ABCB Housing Provisions 2022, AS/NZS 1170.1:2002.
Tell me if you work to anything different.

---

## Priority 1 – site, wind, footings, bracing

These unlock the first three engines (AS 4055 wind class → AS 2870 footing → AS 1684
racking/bracing) and cover the Footing / Floor / Wall sheets.

### AS 4055:2021 – Wind loads for housing

| Ref | What it is | Why the engine needs it |
|---|---|---|
| Cl 1.2 / 1.3 scope limits | Max height, width, roof pitch, Class 1/10a limits | Scope check: stop and flag if the building is outside AS 4055 |
| Fig 2.1 | Wind region map | Tasmania = Region A – confirm the whole practice is Region A so I can default it |
| Cl 2.3 | Terrain category definitions TC1 / TC2 / TC2.5 / TC3 | Engineer picks from a list; definitions shown as help text |
| Cl 2.4 | Shielding FS / PS / NS definitions | As above |
| Table 2.1 *(confirm)* + Figs 2.2–2.3 | Topographic class T0–T5 from slope and position on hill | Deterministic T-class from site slope input |
| Table 2.2 | Wind class for Region A by terrain × shielding × topography | **The wind class lookup** |
| Tables 2.3–2.5 | Same for Regions B, C, D | Optional – only if you ever work outside Region A |
| Table 3.1 *(confirm)* | Design gust wind speeds Vh,u / Vh,s per class | Only for the steel member engine (Priority 4) |

### AS 2870:2011 – Residential slabs and footings

| Ref | What it is | Why |
|---|---|---|
| Table 2.3 | Site class by characteristic surface movement ys | Cross-check the geotech report's class against its ys |
| Cl 1.3 / Table 1.1 / App. C *(confirm)* | Construction-type definitions: clad frame, articulated masonry veneer, masonry veneer, articulated full masonry, full masonry | Maps your "timber frame / brick veneer / double brick" input to the AS 2870 column |
| Table 3.1 + Fig 3.1 | Standard **stiffened raft** designs by site class × construction type (edge/internal beam depth & width, reinforcement, slab thickness, mesh) | Footing engine – slab on ground |
| Table 3.2 + Fig 3.2 | Standard **waffle raft** designs | As above (only if you use waffle rafts – tell me if not) |
| Table 3.3 / Fig 3.3 *(confirm)* | **Footing slab** designs (Class A, S, M) | As above |
| Table 3.4 / Fig 3.4 *(confirm)* | **Strip footing** designs by class × construction type (D, width, reinforcement) | Footing engine – suspended floors (your most common case: 450 W × 450 D, 4-L11TM T&B) |
| Cl 3.1.2–3.1.4 *(confirm)* | Max beam spacing, max slab panel size, beam layout rules | Deterministic internal-beam layout (your IB1 grid) |
| Cl 3.4 / 3.5 *(confirm)* | Pier-and-beam / piered footings and when Class P requires engineered design (Section 4) | Branch: P sites → piers to rock + flag "engineered" (your 5 Bridgeview Rd case) |
| Cl 5.3.1–5.3.3 | Slab thickness, minimum founding depth, edge beam embedment into natural ground | Notes + checks |
| Cl 5.4.3 | Stepped footings | Note text (already in your notes) |
| Cl 5.5 / Table 5.? *(confirm)* | Cover to reinforcement | Your Table 1 already covers this – just confirm source |
| Cl 5.6.? *(confirm)* | Re-entrant corner bars | Your "RE" tag rule (3/N12 2000 long) |
| Cl 6.4.2 | Controlled fill depth limits under slabs (sand / non-sand) | Your slab-on-fill detail quotes two sets of limits (300/150 and 800/400) – confirm which applies where |
| Appendix C, Table C1 *(confirm)* | Articulation joint spacing and placement for articulated masonry | Masonry articulation engine (also see AS 4773.2 below) |
| Appendix D / cl 2.4 | Abnormal moisture / trees – when class is upgraded | Flag only |

### AS 1684.2:2021 – Residential timber-framed construction, Section 8 (bracing)

| Ref | What it is | Why |
|---|---|---|
| Cl 1.4 / Table 1.1 *(confirm)* | Limits of application (storeys, wall height, roof pitch, spans) + **roof mass** definitions (sheet ≤ 40 kg/m², tile) | Scope check; sheet vs tile selects the right tables everywhere |
| Table 8.1 | Racking pressure, **single or upper storey**, N1–N4, by roof pitch × building width W | Racking force engine |
| Table 8.2 | Racking pressure, **lower storey of two storeys**, N1–N4 | Two-storey jobs |
| Table 8.3 | Racking pressure, **sub-floor**, N1–N4 | Suspended floors on stumps/columns (your 5 Bridgeview Rd lower level) |
| Tables 8.4–8.6 *(confirm)* | Same for C1–C3 | Optional – skip unless you need cyclonic |
| Cl 8.3.4 + Fig 8.2 | How to take projected elevation area, half-height rule, hip vs gable, each direction | Logic I implement – I need the exact wording |
| Table 8.18 | **Structural bracing wall types** (a)–(j): description, capacity kN/m, minimum panel length, method A/B | Bracing selection – your d / g / h(B) types |
| Table 8.19 *(confirm)* | Nominal wall bracing capacity | Credit for nominal walls if you use it (tell me if you don't) |
| Cl 8.3.6 (+ its table of max spacing) | Distribution rules: max spacing between bracing walls, distance from corners, both directions, even distribution, lower-storey rules | Panel placement algorithm |
| Table 8.20–8.22 *(confirm)* | Fixing of top of bracing walls to roof/ceiling, bottom plate fixing for bracing panels | Standard notes and the "M12 at each end of bracing panels" rule you already use |

---

## Priority 2 – member sizing (AS 1684.2 Supplements)

You specify **F7 TP** (external bearers, deck joists, verandah beams), **F17/LVL**
(lintels, joists, internal bearers, double studs) and occasionally MGP10 (noggings).
So I need the Supplement span tables for:

- **F7 seasoned softwood** – N1/N2 and N3/N4 supplements
- **F17 seasoned hardwood** – N1/N2 and N3/N4 supplements
- MGP10 / MGP12 only if you ever specify them for structural members (looks like no)

For LVL: your schedules say "F17/LVL" as one item. I'll treat the F17 table as governing
and keep LVL as an "or equivalent" note unless you want manufacturer LVL tables as well.

Member tables needed from each supplement (sheet roof rows at minimum; tile if you do them):

| Member | Table inputs | Used on sheet |
|---|---|---|
| Bearers – supporting floor only, and floor + load-bearing wall / roof | FLW, RLW, span, continuous vs single | Floor |
| Floor joists (also used for deck joists) | FLW/spacing, span, continuous vs single | Floor |
| Lintels – single/upper storey, and lower storey | RLW, span, roof type, FLW for lower storey | Wall |
| Verandah beams | RLW, span | Roof / Wall |
| Rafters | spacing, span, roof mass | Roof (skillion / cathedral only; trusses stay "by manufacturer") |
| Underpurlins | spacing/RLW, span | Roof |
| Ridge / intermediate beams | RLW, span | Roof |
| Hanging / strutting beams, ceiling joists | span, CLW | Roof (rarely) |
| Common studs, jamb studs, top plates | height, spacing, RLW | Wall notes ("F17 studs to openings > 1500" etc.) |
| Posts | load, height | Columns where timber (you use SHS – low priority) |

Also from the body of AS 1684.2:

| Ref | What | Why |
|---|---|---|
| Section 2 Figs 2.x | Definitions and figures for **FLW, RLW, CLW, uplift load width** | Tributary-width calculation from plan geometry |
| Cl 4.x | Joist spacing limits, blocking, bearer continuity rules | Floor framing rules (your "300 ctrs under tiled areas") |
| Cl 6.x + Table 6.? jamb studs *(confirm)* | Lintel bearing, **jamb stud** sizes by opening width / RLW / grade, stud height limits | Settles the F17-vs-F7-TP jamb-stud rule seen across the sets; wall framing notes |
| Cl 7.x | Roof framing rules | Roof |

---

## Priority 3 – tie-down and masonry

### Tie-down

Your general notes already carry a derived table – **"Table 4: Tie down for N2 (and N3)
sites, single or upper storey, sheet roof"**. Two options:

1. **Encode your Table 4 variants as the house standard** (fast; fully repeatable). I have
   the N2 and N3 versions from these jobs. Send any others you keep (N1, N4, tile roof,
   lower storey of two storeys). Combinations you don't have get flagged.
2. Derive from AS 1684.2 Section 9 (Table 9.1 uplift forces by wind class / roof type /
   load width, Tables 9.2–9.26 connection capacities, Fig 9.5 uplift width).

My recommendation is option 1 now, option 2 later if needed. Either way I need **Fig 9.5**
so the engine can compute uplift load width.

### Masonry (brick veneer / double brick)

| Ref | What | Why |
|---|---|---|
| Source of your **Masonry lintel schedule** (<900: 75×10 FL; 900–1500: 100×75×8 UA; 1500–3300: 150×100×10 UA; 150 bearing) | Probably ABCB Housing Provisions Part 5.6 steel lintel table or AS 4773 | So the trace can cite it; and to extend it if a job has a wider opening |
| AS 4773.2:2015 cl on **control / articulation joint spacing** (and AS 2870 App. C) | Joint spacing by site class, distance from openings/corners | Articulation engine – draws CJ/AJ on the wall sheet for masonry jobs |
| ABCB HP cl 5.6.5 | Wall tie spacing | Note text (already in your notes) |
| ABCB HP cl 5.2.6 / 5.5.6 *(confirm numbering)* | Engaged piers (1600/1800 ctrs) and isolated piers (230×230 to 1500, 350×350 to 2400) | Your foundation-brickwork callouts |
| ABCB HP Table for steel lintels, full masonry | If you do double-brick lintels differently from veneer | Double brick jobs (10 %) |

---

## Priority 4 – steel members, loads, piers (decision needed)

Every job has steel: 89×3.5 / 89×5 SHS columns, 250 PFC lintels/roof beams over
stacker doors, 250UB37 / 310UB40 first-floor bearers. These are outside the
deemed-to-satisfy tables. Options:

- **A. Lookup matrix from your own practice** – span × load width → section, which you
  populate (or I seed from these three jobs and you extend). No calculation.
- **B. Small AS 4100 elastic beam/column engine** – bending, shear, deflection with
  section properties from the public Liberty/BlueScope catalogues. Needs your design
  criteria: deflection limits, load combinations, roof/floor dead loads. Deterministic,
  ~a day's work, and it gives the trace a real calculation.

If B: I also need **AS/NZS 1170.1 Table 3.1** rows you use (residential floor, balcony,
garage, roof) and your standard dead-load assumptions (sheet roof, tile roof, timber
floor, tiled floor), or your existing load-assumption sheet if you have one.

Piers: your rule "Ø450 bored pier to rock/refusal at corners and max 3000 ctrs, 1/N16
central" and the bearing check behind it. Tell me if there's a bearing-pressure or pier
load table you use, or whether this is purely the house rule.

---

## Citations to verify in the notes library

These appear in your current notes and mix old BCA and new Housing Provisions numbering.
Once confirmed I'll fix them in the notes library so every job is consistent:

- "BCA 3.4.4.4 Table 3.4.4.2" (steel corrosion protection) vs "NCC cl 6.3.9 of ABCB Housing Provisions"
- "Table 3.1.1.1 of NCC 2022 Volume 2" (embankment slopes) – HP 2022 numbering
- "Provide sub-floor ventilation, flashing and damp-proof course to AS 3798" (Schouten St) vs "to AS 4773" (later jobs) – the later one looks correct
- "Part 4.2 of ABCB Housing Provisions" (compacted fill)
- CSIRO "Technology File 18" → BTF 18 "Foundation maintenance and footing performance"

---

## Not needed from you

- Geotech-report parsing: stays with Claude, but outputs a fixed site model (class, ys,
  bearing, founding depth, fill, groundwater, trees, recommendations) that you review.
- Drawing-geometry extraction: vector lines + dimension text from the PDF; Claude only
  labels (which lines are walls, which are load-bearing, where the roof breaks).
- Your general-notes sheets, standard details and plan-note wording: already captured
  from these three jobs into `app/data/house/` – see `docs/house-style-extracted.md`.
