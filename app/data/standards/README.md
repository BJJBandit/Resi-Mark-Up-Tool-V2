# Standards tables

Deterministic lookup data for the design engines. One table per file, grouped by standard:

```
standards/
  as4055_2021/     wind classification
  as2870_2011/     footings and slabs
  as1684_2_2021/   timber framing – bracing, tie-down, member rules
  as1684_2_supp/   span tables (CSV) per grade and wind class
  ncc_2022_hp/     ABCB Housing Provisions – masonry, lintels, piers
  as4773_2015/     masonry articulation
```

Rules:

- Values are transcribed from Arete's licensed copies. **Nothing is filled in from memory.**
  A table with an empty `rows:` list makes the engine report "table not loaded" rather
  than guess.
- Each file carries `standard`, `edition`, `table`, `title`, `inputs`, `outputs`, `units`
  and `source_checked_by`. The per-job trace quotes these, so keep them accurate.
- Small matrices: YAML with `rows:` as a list of maps.
- Span tables: CSV with one row per (inputs…) → outputs, plus a YAML sidecar of the same
  name holding the metadata. Keep the column names identical to the standard's own
  headings so checking against the page is mechanical.
- Edition changes: add a new folder (e.g. `as2870_2024/`) rather than editing in place,
  and switch the active edition in `app/data/house/preferences.yaml`.

See `docs/standards-data-required.md` for the list of tables still to be supplied.
