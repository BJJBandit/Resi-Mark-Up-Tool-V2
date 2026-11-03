# `app/calc` – steel member engine (AS 4100)

Deterministic elastic checks for the practice's steel palette: **UB floor bearers**
(250UB25.7 … 310UB46.2), **PFC lintels / roof beams** (200, 250, 300 PFC) and **SHS posts**
(75×4, 89×3.5, 89×5, 100×4, 100×5). Decision of 3 Nov 2026: option B, a small engine plus a
section catalogue, not a lookup matrix. Plain functions, pydantic results, clause next to
every constant and formula. Internal units N·mm; inputs/outputs kN, kN/m, kNm, mm.

## Files
| file | what |
|---|---|
| `sections.py` | loads `app/data/standards/steel_sections/{ub,pfc,shs}.yaml` → `Section`; `get_section("250 PFC")` |
| `steel.py` | `beam_actions`, `section_moment_capacity`, `member_moment_capacity`, `shear_capacity`, `check_beam`, `design_beam`, `section_compression_capacity`, `member_compression_capacity`, `check_column`, `design_column`, `CONSTANTS` |
| `check_sections.py` | catalogue self-consistency (Ag, mass, Zx, rx, ry, Ix recomputed from plate dimensions); `python -m app.calc.check_sections` |

## Method
**Loads** (AS/NZS 1170.0). Strength `1.2G + 1.5Q` (cl 4.2.2(b)); serviceability `G + ψs·Q`
and `G + ψl·Q` with ψs = 0.7, ψl = 0.4 (Table 4.1, residential). Simply supported: M*/V*
from a statics envelope of UDL + point loads; mid-span deflection by superposition
(`5wL⁴/384EI`, `Pa(3L²−4a²)/48EI`). Two-span continuous (equal spans, UDL only):
`M = wL²/8` at the central support, `V = 5wL/8`, `δ = wL⁴/185EI`. Self-weight of the trial
section is added to G in `check_beam` (switchable).

**Bending** (AS 4100 Section 5). Plate slenderness `λe = (b/t)√(fy/250)` (cl 5.2.2) against
Table 5.2 limits (HR for UB/PFC, CF for SHS). Clear widths: UB flange outstand `(bf−tw)/2`,
PFC `bf−tw`, web `d−2tf`, SHS `B−2t` – the root radius is *not* deducted (cl 5.2.2; the
brief's `−r1` would be slightly unconservative). `Ze`: compact `min(S, 1.5Z)` (cl 5.2.3),
non-compact interpolation (cl 5.2.4), slender `Z·λsy/λs` (cl 5.2.5(a)). `φMs = 0.9·fy·Ze`.
Member capacity `φMb = φ·αm·αs·Ms ≤ φMs` (cl 5.6.1.1): `restraint="full"` → αs = 1;
`restraint="segment"` → `Le = kt·kl·kr·L` (defaults 1.0 / 1.4 top-flange load / 1.0, Tables
5.6.3(1)–(3)), `Mo` from eq 5.6.1.1(3), `αs` from eq 5.6.1.1(2). `αm` defaults to 1.0
(Table 5.6.1 values offered in `TABLE_5_6_1_EXAMPLES`). fy for bending is the lower of the
flange/web yield (Table 2.1 by thickness: 320 / 300 / 280 MPa for Grade 300; 350 for C350L0).

**Shear** (cl 5.11). `Vw = 0.6·fy·Aw`, `Aw = d·tw` (UB/PFC) or `2·d·t` (SHS); web buckling
`αv` per cl 5.11.5.1 when `dp/tw > 82/√(fy/250)`; shear–moment interaction cl 5.12.3.

**Deflection.** Total `δG + ψs·δQ` and live `ψs·δQ` against limits parsed from the
preference strings (`"span/300"`, `"span/360, max 10 mm"`) – AS/NZS 1170.0 App. C. The
limits in `preferences.yaml` are placeholders still to be confirmed by the practice.

**Columns** (Sections 6 and 8). `kf` from effective widths (cl 6.2.4, Table 6.2.4);
`φNs = φ·kf·An·fy` (cl 6.2.1); `φNc = φ·αc·Ns` (cl 6.3.3) with `αb = −0.5` for cold-formed
SHS (Table 6.3.3(1)), `Le = ke·L` (Fig 4.6.3.2, default 1.0 pinned). Combined actions for a
small moment / eccentricity: cl 8.3.2 (`Mrx = Msx(1 − N*/φNs)`), cl 8.3.4 biaxial when My*
is given, cl 8.4.2.2 in-plane and 8.4.4.1 out-of-plane (`Mix`, `Mox`). Second-order
amplification (cl 4.4.2.2) is not applied; the trace flags `N*/φNc > 0.9`.

**Selection.** `design_beam` / `design_column` walk the family lightest → heaviest and return
every section's utilisation; the first adequate one is `selected`. Each result's `trace`
quotes the catalogue `source` and says **"section properties unverified"** when the row has
`verified: false` – currently *all* rows, see the YAML headers.

## Assumptions
Prismatic, straight, single-span or two equal spans; loads gravity-only (no wind uplift or
reversal combinations); loads applied to the top flange; supports provide full twist
restraint (FF); steel E = 200 000 MPa, G = 80 000 MPa; no holes (An = Ag); mid-span
deflection taken as the maximum.

## Not covered
Web bearing / crushing and bearing buckling (cl 5.13 – `TODO` entry in every beam result),
web stiffeners, composite action with slabs/decking, fatigue, connections (cleats, bolts,
welds, base plates – the house callouts cover these), fire, corrosion protection, lateral
loads on columns, biaxial bending of beams, cantilevers, point loads on continuous spans,
moment amplification, torsion.
