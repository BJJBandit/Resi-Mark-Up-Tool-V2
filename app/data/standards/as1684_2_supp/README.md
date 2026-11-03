# AS 1684.2 Supplement span tables

One CSV per member type per grade per wind-class pair, with a YAML sidecar for metadata.
Naming: `<grade>_<wind>_<member>.csv`, e.g. `F17_N1N2_lintels_single_upper.csv`.

Column conventions (match the Supplement headings):

| Column | Meaning | Units |
|---|---|---|
| `roof_type` | `sheet` or `tile` | – |
| `rlw` | roof load width | mm |
| `flw` | floor load width | mm |
| `clw` | ceiling load width | mm |
| `spacing` | member spacing | mm |
| `size` | section, e.g. `190x45`, `2/190x45` | – |
| `span_single` | max single span | mm |
| `span_continuous` | max continuous span | mm |

Leave a cell blank where the Supplement shows a dash. Add columns rather than overloading
existing ones if a table has extra parameters (e.g. `storeys_supported`).

Template sidecar:

```yaml
standard: AS 1684.2 Supplement
edition: 2021
supplement: ""          # e.g. "Supplement 9 – F17 seasoned hardwood, N1/N2"
table: ""               # e.g. "Table 15 Lintels – single or upper storey"
title: ""
grade: F17
wind_classes: [N1, N2]
member: lintels_single_upper
inputs: [roof_type, rlw, size]
outputs: [span_single, span_continuous]
units: {rlw: mm, span_single: mm, span_continuous: mm}
source_checked_by: ""
```
