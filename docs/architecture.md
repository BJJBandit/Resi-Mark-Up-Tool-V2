# Architecture (v2 rebuild)

Goal: consistent, repeatable mark-ups; a design process you can read; tokens spent only
where judgement is needed.

```
          PDFs                         Stage 1 (tokens: small)                 Engineer
  ┌──────────────────┐   ┌───────────────────┐  ┌──────────────────────┐   ┌──────────────┐
  │ architectural    │──►│ extract (vectors, │─►│ label (Claude,       │──►│ MODEL REVIEW │
  │ geotech report   │   │ text, dims, scale)│  │ Sonnet): load-bearing│   │ edit/approve │
  └──────────────────┘   └───────────────────┘  │ walls, roof, geotech │   └──────┬───────┘
                                  no LLM        └──────────────────────┘          │
                                                                                  ▼
          Stage 2 (tokens: none)                                        ┌──────────────────┐
  ┌──────────┐  ┌──────────────────┐  ┌──────────┐  ┌───────────────┐   │ outputs          │
  │ planner  │─►│ engines (tables, │─►│ layout   │─►│ annotate + pdf│──►│ markup.pdf       │
  │ (graph)  │  │ decision trees)  │  │ (geometry│  │ (notes library│   │ trace (json/md/  │
  └──────────┘  └──────────────────┘  └──────────┘  └───────────────┘   │   mermaid)       │
                        │ every step → TRACE                            │ review.json      │
                                                                        └──────────────────┘
```

| Package | Role | LLM? |
|---|---|---|
| `app/model/` | The structural model (`Project`): site, buildings, storeys, walls, openings, roof, floors, pages, review state. Every extracted value carries provenance + confidence. | – |
| `app/extract/` | PDF → model geometry: vector segments, dimension chains, scale, walls, openings, page kinds, title-block facts. Alerts when a page is image-only (fallback "eyeball" mode is opt-in). | no |
| `app/label/` | Fills judgement fields: deterministic roof text first; then Claude per storey (load-bearing by wall ID on a labelled plan image) and per geotech report. Token totals per job. | yes (Sonnet, medium) |
| `app/design/planner.py` | Fixed dependency graph → ordered engine runs per site/building/storey + sheet set per building. Skipped steps keep their reason. | no |
| `app/engines/` | Standards engines (AS 4055 wind, AS 2870 footings, AS 1684 racking/members/tie-down, AS 4773 articulation) over YAML decision trees and tables; each step recorded in the trace with citation. Tables hold no values until the engineer loads them. | no |
| `app/calc/` | AS 4100 elastic beam/column checks with a section catalogue. | no |
| `app/design/layout.py` | Piers along footings, bracing panels along eligible segments, lintel tags – geometry only. | no |
| `app/data/house/` | Preferences, notes library, standard callouts, (later) details library. Editable in the app. | – |
| `app/data/standards/` | Tables transcribed from licensed standards, with provenance. | – |
| `app/logic/` | Decision trees as YAML; rendered to Mermaid on `/design-logic`. | – |
| `app/pdf.py` | Draws the A3 mark-up sheets as PDF annotations (kept from v1). | no |

## Principles

1. **Standards beat preferences.** Where a preference would be less conservative than a
   loaded table, the table wins and the trace says so.
2. **Nothing invented.** A missing table or constant makes a step `blocked` with the
   citation it needs; the UI shows the practice proposal as "unverified".
3. **Same input, same output.** Stage 2 has no randomness and no LLM; the regression
   fixtures (`fixtures/manifest.yaml`) hold the expected results.
4. **Everything cites.** Every number on a sheet can be followed back through the trace to
   a table row, a clause, a preference entry, or an engineer's edit.

## Where tokens go

| Call | Model | Context | Typical size |
|---|---|---|---|
| Wall / roof labels, one per storey | Sonnet | labelled plan image + wall list + roof text facts (+ 1–2 low-res context pages) | ~3–6k in, <1k out |
| Geotech report | Sonnet | the report PDF | report size + ~0.5k out |
| (optional) adapt a standard callout | Sonnet | note template + job facts | <1k |

Compared with v1 (7 Opus/high calls re-sending all page images + reference PDFs), a
three-unit job drops from several hundred thousand input tokens to well under fifty.
