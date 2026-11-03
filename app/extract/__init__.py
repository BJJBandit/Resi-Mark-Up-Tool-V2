"""Deterministic geometry extraction from architectural PDFs.

No LLM calls live in this package. See README.md in this folder for the algorithm
and its failure modes. Entry point: `extract.extract_project(pdf_bytes)`.
"""

from .extract import extract_project  # noqa: F401
