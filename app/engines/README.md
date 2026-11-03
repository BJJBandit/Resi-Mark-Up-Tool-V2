# Design engines

Deterministic replacements for the routine design decisions. No LLM calls; every number
comes from a standards table the engineer has transcribed and checked, every branch is data
the engineer can read, and every decision is written to a trace they can audit.

```
app/engines/
  tables.py    loads app/data/standards/**/*.yaml (+ .csv sidecars) → Table.lookup(**inputs)
  trace.py     Trace / Step: what each engine did, why, citing what; JSON · Markdown · Mermaid
  logic.py     YAML decision trees (app/logic/*.yaml): validate · walk · render as Mermaid
  prefs.py     preferences.yaml access + conservative_merge (preference may never beat the standard)
  wind.py      AS 4055:2021 wind classification
  footings.py  AS 2870:2011 footing-system selection
app/logic/     one YAML tree per engine
app/routes_logic.py   GET /design-logic (page) · /design-logic/{engine}.mmd · /tables/status
```

## The three rules

1. **Tables hold the values, and start empty.** A table whose `rows: []` raises
   `TableNotLoaded`, which an engine records as a `blocked` step quoting the citation
   ("AS 2870:2011 Table 3.4 – not loaded"). Nothing is filled from memory. Lookups read
   numeric inputs conservatively (`numeric_rule: ceil` = next tabulated bound at or above
   the demand) unless the table says otherwise; interpolation only when `interpolate: true`;
   a blank cell (dash) is `OutOfRange`.
2. **Logic is data.** The branching for each engine lives in `app/logic/<engine>.yaml` with
   five node types – `ask` (decision), `result`, `lookup`, `end`, and `flag`/`status` on any
   of them. `logic.walk()` interprets it, recording each node into the trace with its
   `node_id`; `logic.tree_to_mermaid()` draws the whole tree, highlighting the path taken.
   Python code computes derived inputs (e.g. wall construction → AS 2870 construction type)
   and packages the result; it does not hide branches.
3. **Preferences never beat a standard.** Practice standards (`preferences.yaml`) are
   *proposals*. Where the standard table is loaded, `prefs.conservative_merge` adopts the
   larger depth / width and writes a `conservatism_note`; where it is not loaded, the
   proposal is carried with `preference_applied` set and the lookup step is `blocked`, so the
   UI can show "proposed per practice standard – Table X not yet loaded to verify".

Statuses: `ok` · `flag` (engineer must look) · `blocked` (table or input missing, citation
says which) · `not_supported` (practice decision: tile roofs, waffle rafts, cyclonic regions).

Values from a standard that are *limits or factors* rather than tables (e.g. AS 4055 cl 1.2
height / width / pitch) are named constants set to `None` with a
`TODO(standard: AS xxxx cl y.y)`; the step that needs them is `blocked` until they are
confirmed from the licensed copy.

## Running an engine

```python
from app.engines import wind, footings
from app.engines.tables import default_tables
from app.engines.prefs import load_preferences
from app.engines.trace import Trace

trace = Trace(project_name=project.project_name, job_no=project.job_no, building_id="B1")
w = wind.run(project, "B1", default_tables(), load_preferences(), trace)
f = footings.run(project, "B1", default_tables(), load_preferences(), trace)
trace.to_json()      # for review.json
trace.to_markdown()  # review page
trace.to_mermaid()   # steps taken + load path chain
trace.blocked_on()   # ["AS 4055:2021 Table 2.2", "AS 2870:2011 Table 3.4", ...]
```

Load paths (roof → walls → footing → piers) are `load_path` steps in the same trace – the
practice decided they are not issued as a sheet.

## Adding an engine

1. **Tree** – write `app/logic/<engine_id>.yaml` (copy `footings_as2870.yaml`). List the
   `tables:` it depends on; add table *templates* (empty `rows: []`, correct citation,
   inputs/outputs, `numeric_rule`) under `app/data/standards/<standard>/` for any new ones.
2. **Module** – `app/engines/<name>.py` with `ENGINE_ID`, `LOGIC_FILE`, a pydantic `Result`
   and `run(project, building_id, tables, prefs, trace) -> Result`. Open a
   `trace.section(...)`, record derived inputs as steps, `walk()` the tree, build any
   practice proposal from `prefs` and merge it with the table row via `conservative_merge`.
   Constants you need from the standard: `None` + `TODO(standard: …)`, and block on them.
3. **Register** – add it to `ENGINES` in `app/engines/__init__.py`; the logic page and
   `tests/test_engines.py::test_repo_trees_load_and_registry_matches` pick it up.
4. **Tests** – one per branch that matters, plus "table not loaded → blocked with citation"
   and "table deeper than preference → table wins".

## Logic YAML in one screen

```yaml
engine: my_engine
title: What it decides (AS xxxx Section n)
standard: AS xxxx:yyyy
inputs: [a, b]                      # variables the engine passes in
tables: [as_xxxx/table_n]           # dependencies, for the logic page
start: first
nodes:
  - id: first
    ask: a                          # decision on variable a
    citation: AS xxxx cl 1.2
    branches:
      - when: {in: [x, y]}          # in · not_in · eq · ne · lt · le · gt · ge · missing · same_as · differs_from
        goto: pick                  #   (add var: other to test a different variable)
      - else: true
        goto: stop
  - id: pick
    result: {system: S1, proposal_key: s1}   # {var: name} copies a variable
    preference: footings.something  # recorded as preference_applied
    flag: Engineer to confirm …     # status flag
    goto: look
  - id: look
    lookup: as_xxxx/table_n
    with: {col: a, other: b}        # table input ← variable (or literal)
    into: standard_design           # results[into]; outputs also become variables
    on_not_loaded: blocked          # or not_supported
    goto: stop
  - id: stop
    end: true                       # optional status: blocked / not_supported, rule, citation
```
