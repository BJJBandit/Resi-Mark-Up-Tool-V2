"""Labelling stage: the only place the new pipeline spends tokens.

Extraction (`app.extract`) has already produced geometry from the PDF's vectors and
text. What's left needs judgement, not measurement:

- which internal walls are load-bearing, and what the roof is (`walls.py`)
- what the geotechnical report says (`geotech.py`)

Each call sends the smallest useful context (one labelled plan image, a few hundred
words of extracted text) to a cheaper model and gets back a flat JSON object. Results
are written into the `Project` with `Provenance(source="claude")` and a confidence,
so the review screen shows them as Claude's suggestions, not facts.

Deterministic parsing that *looks* like labelling (roof pitch written on an elevation,
"Colorbond" in a note) lives in `roof_text.py` and runs first; Claude only fills gaps.
"""

from .llm import Usage, UsageTotals  # noqa: F401
from .pipeline import label_project  # noqa: F401
