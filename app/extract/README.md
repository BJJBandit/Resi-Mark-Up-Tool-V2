# Geometry extraction (`app/extract`)

Reads an architect's PDF and produces the geometry part of the structural model
(`app/model/schema.py`): pages, scale, walls with openings, the building footprint
and the site data printed in the title block. Everything here is deterministic:
no LLM is called, and whatever cannot be settled from the vectors and text is
written to `review.needs_attention` for the engineer rather than guessed.

Entry point: `extract_project(pdf_bytes) -> (Project, diagnostics)`.
Debug overlay: `python -m app.extract.debug <pdf> <page> [out_dir]`.

## Coordinate frames

* **Page points, y up.** All geometry inside this package uses points with the
  origin at the bottom-left corner of the *visible* page (`page.rect`), i.e.
  `(x, page.rect.height - y_pymupdf)`. We do not use `page.transformation_matrix`
  because PDFs with an off-centre MediaBox (one of the fixtures) make it return
  nonsense. `PlanFrame.origin` is expressed in this frame.
* **Building-local mm.** `Wall.start/end`, `Opening.offset_mm` and the footprint
  are in millimetres, x right, y up, from the bottom-left corner of the plan's
  outer wall faces. `page_pt = frame.origin + mm * frame.pts_per_mm` (rotation is
  always 0 - plans are axis-aligned, diagonal walls are not detected).

## Steps

### 1. Page scan (`pdf_pages.py`)

For every page: text with positions (lines and words, with orientation), the
sheet size from the page rectangle (A4..A0 within 3 %), whether the page is
vector (at least 30 drawing items and not a single raster image covering most
of the sheet), every scale mention `1:100` / `1 : 100 @ A2` with its position
(non-standard ratios such as the `1:60 - 1:80` of a shower fall are ignored),
the page kind guessed from the drawing title, and the title-block key/values.

Title-block values are found by position: the nearest text to the right of a
label on the same row, or the line immediately below when it is left-aligned
with the label (both layouts occur). Only the bottom and right 22 % of the sheet
is searched, so sheet lists and notes do not pollute the result.

*Fails when:* the scale is drawn as outlines or an image (the dimension check
below covers that); the title block uses a layout with values far from their
labels; a title we have not seen ("GF LAYOUT") is classed `other`.

### 2. Vectors (`vector.py`)

`page.get_drawings()` becomes three lists: straight strokes snapped to the axes
and merged along their line (only segments of the same pen width and kind are
merged, so a light window line never bridges a heavy wall line), thin dark
filled rectangles (`bars`, how some offices draw walls), and bezier curve boxes
(door swings). Pen widths are tallied so the wall pen can be recognised.

### 3. Dimensions and scale (`dimensions.py`)

A dimension is a 2-5 digit number sitting just off a thin line of the same
orientation. Tick marks are the short segments crossing that line; the number's
tick interval, in points, divided by its millimetres gives points per mm. All
numbers on the page vote; the largest cluster of votes agreeing within 1.5 % is
the page's measured scale (`72 / 25.4 / ratio`). Consistent dimensions on one
line form a chain; where a single dimension spans the chain's full extent its
value is checked against the chain's sum. The largest consistent dimension in
each direction is the overall extent.

*Fails when:* dimension text is outlined rather than text; a dimension line is
crossed by other geometry (wrong interval, becomes an outlier); a page with
details at a second scale produces a second cluster (reported).

### 4. Walls (`walls.py`)

1. **Wall bars** - one of two evidence paths per page:
   * *fill mode*: if the page has ≥ 15 m of thin dark filled bars 60-320 mm
     thick, those bars are the walls; solid shapes that are not plain rectangles
     (returns, nibs) are recovered by pairing the edges of the filled polygons.
   * *pair mode*: otherwise walls are two parallel lines drawn with the heaviest
     pen that carries a meaningful share of long lines. Every pair at 60-320 mm
     spacing that overlaps along its length is a candidate. A pair with another
     heavy line inside its band is accepted only if the band is hatched (a brick
     leaf), otherwise the inner line is a wall of its own or joinery. Evenly
     spaced repeats (stairs, tiles, hatching) are rejected. Candidates are
     accepted greedily - nominal thickness first, thicker first, longer first -
     and each accepted wall consumes its two lines and its band, so the brick
     skin of a brick-veneer wall is not reported twice. Non-nominal pieces
     shorter than 1.5 m are discarded as joinery.
2. **Runs** - collinear bars of like thickness are chained. A gap is bridged
   silently when it is drafting noise (≤ 70 mm) or a cross wall meets it there;
   it is an opening when a tag of matching width sits by it, when window lines
   span it, when a door swing touches it, or (≤ 2.5 m, no evidence) when it is
   not boxed in by cross walls at both ends; otherwise the run ends.
3. **Run ends** - an opening beside a corner leaves the run short. A run is
   stretched to the cross wall it faces when there is opening evidence across the
   gap, or as far as window lines (two parallel light lines in the band) go.
4. **Glazed walls** - a side of a room that is all glass has no wall bar: when a
   wall ends on a line and window lines cross from there to another wall, a run
   that is one long opening is synthesised so the outline closes.
5. **Corners** - run ends are snapped to the centre-line of the cross wall they
   meet.
6. **Plans** - the union of wall footprints (shapely) splits the page into
   clusters; each at least 2.5 m x 2.5 m with ≥ 4 walls is a plan. Two plans on
   one page (ground and first floor side by side) come out separately, each
   labelled from the nearest underlined title (`GROUND FLOOR`), else the drawing
   title.
7. **External walls** - open air along a face: a point just outside that face
   falls outside the cluster's filled outline. A wall that is external along only
   part of its length (the return beside a deck) is split at the junction where
   the status changes. External walls get `load_bearing=True`; internal walls
   keep `None` for the next stage.
8. **Windows in continuous walls** - brick-veneer windows where only the brick
   line and the light window lines break: two or more light lines inside an
   external wall's band over ≥ 300 mm are an opening.
9. **Tags** - every opening takes the nearest unused tag text (`A1524`,
   `15-24AW (DG)`, `820`, `820 CSD`, `21-27 Roller Door`, `W05*`) whose encoded
   width, if any, matches the gap within 35 %. Tag width overrides the measured
   gap; the measured gap is kept in the opening's provenance note.

Nominal thicknesses: 90, 110, 140, 190, 230, 250, 270, 290 mm ± 15 mm (90 and
250 preferred when two match). Anything else is kept as measured and flagged.

*Fails when:* walls are drawn with the same pen as furniture and no fill
(neither mode applies; the page yields nothing and is flagged); a perimeter has
a side with no wall at all (open carport) so the outside leaks in and internal
walls near it are called external (`closed=False` is flagged); walls are
curved or diagonal; a tag convention we have not seen is used (reported as
unmatched, never guessed); joinery drawn with the wall pen at a nominal spacing
(robes, benches) can appear as internal walls - these carry a non-nominal
thickness note where possible and must be deleted in review.

### 5. Project assembly (`extract.py`)

Scale per plan page: the measured scale when at least three dimensions agree
(disagreement > 2 % with the printed scale is flagged), else the title-block
scale, else any printed scale, else none (flagged). Buildings are keyed by
`UNIT n` in the title or plan label, else `house` (`garage`, `shed`, `existing`
when named); storeys by `GROUND`/`FIRST`/`SECOND`/`SUBFLOOR` words. Site data
comes from the title block (`Soil Classification: S`, `Wind Speed: N3`,
`Certified BAL: 29`, `Corrosion Environment: MEDIUM`, `Climate Zone: 7`) with
`Provenance(source="pdf_text", page=n)`.

`needs_attention` lists: no scale / scale disagreement, raster plan pages,
non-nominal wall thicknesses, unmatched opening tags, plans whose external
walls do not close, duplicate storeys, missing site or wind class, and the
internal walls awaiting a load-bearing decision.

## Testing

`pytest tests/test_extract.py` runs against the fixture PDFs (skipped when
absent). The visual acceptance test is the overlay from `app.extract.debug`:
green walls (thick = external), red openings, blue dimension chains, magenta
plan frame.
