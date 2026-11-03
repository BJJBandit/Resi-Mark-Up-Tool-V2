"""The structural model: a fixed, reviewable description of the site and the building.

Stage 1 (extraction) fills it from the PDFs - vector geometry and title-block text
deterministically, Claude only for labelling and the geotech report. The engineer
reviews and corrects it in the browser. Stage 2 (design) reads it and never calls an LLM.

Every quantity is in millimetres, kPa, kN or degrees. Plan coordinates are in a
building-local frame (mm, x to the right, y up) that `PlanFrame` maps to a PDF page.
"""

from .schema import *  # noqa: F401,F403
