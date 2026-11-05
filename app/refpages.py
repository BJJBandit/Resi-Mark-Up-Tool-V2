"""Send Claude only the pages of each standard / guide that the review needs.

For a document whose 'Pages to review' box is left blank, read its bookmarks,
its contents pages and the first lines of every page (the 'page map', which
shows where each section really starts even when printed page numbers differ
from PDF page numbers), ask Claude which sections this job needs, and extract
just those pages. Pages typed on the form are used as given; 'all' sends the
whole document. Every document's pages are reported back to the engineer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from .pdf import extract_pages, format_ranges

MAX_REF_PAGES = 500  # Claude reads at most 600 PDF pages per request; leave room
SMALL_DOC_PAGES = 25  # short documents are sent whole; no need to index them
CONTENTS_SCAN_PAGES = 40  # look for contents pages in the front of the document
MAX_CONTENTS_PAGES = 12
SNIPPET_CHARS = 150


@dataclass
class RefDoc:
    name: str
    data: bytes
    page_count: int
    pages: list[int] | None = None  # None = choose automatically from the contents
    how: str = ""
    sections: list[dict] = field(default_factory=list)
    store: str = ""  # how the pages were read: stored extracts / newly extracted / PDF

    @property
    def stem(self) -> str:
        return self.name.rsplit(".", 1)[0]


def page_map(data: bytes) -> tuple[str, str, str, bool]:
    """(bookmarks, contents-page text, per-page map, has a text layer)."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        toc = doc.get_toc(simple=True)  # [level, title, page]
        bookmarks = "\n".join(f"{'  ' * (lvl - 1)}{title.strip()} -> p{p}" for lvl, title, p in toc if p > 0)
        texts = [pg.get_text("text") for pg in doc]
    has_text = sum(len(t.strip()) > 40 for t in texts) >= max(1, len(texts) // 4)

    contents = []
    for i, t in enumerate(texts[:CONTENTS_SCAN_PAGES]):
        if re.search(r"\b(contents|table of contents)\b", t, re.I) or len(re.findall(r"\.{4,}\s*\d+", t)) >= 5:
            contents.append(f"--- p{i + 1} ---\n{t.strip()}")
        if len(contents) >= MAX_CONTENTS_PAGES:
            break

    lines = []
    for i, t in enumerate(texts):
        ls = [ln.strip() for ln in t.splitlines() if ln.strip()]
        if not ls:
            lines.append(f"p{i + 1}: (no text)")
            continue
        head = " | ".join(ls[:3])
        foot = ls[-1] if len(ls) > 3 else ""
        snippet = (head + (f" || {foot}" if foot else ""))[:SNIPPET_CHARS]
        lines.append(f"p{i + 1}: {snippet}")
    return bookmarks, "\n\n".join(contents), "\n".join(lines), has_text


def prepare(
    docs: list[RefDoc],
    context: str,
    select: Callable[[RefDoc, str, str, str, str, int], list[dict]],
    budget_total: int,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Decide the pages for every document and extract them.

    select(doc, bookmarks, contents, page_map, context, budget) -> list of
    {"title", "pages": [int], "why"} chosen by Claude.
    Returns ([(upload name, pdf bytes)], warnings). Fills doc.pages / how / sections."""
    warnings: list[str] = []
    manual = sum(len(d.pages) for d in docs if d.pages is not None)
    auto = [d for d in docs if d.pages is None]
    remaining = max(0, budget_total - manual)
    per_doc = max(10, remaining // max(1, len(auto))) if auto else 0

    for d in auto:
        if d.page_count <= min(SMALL_DOC_PAGES, per_doc):
            d.pages, d.how = list(range(1, d.page_count + 1)), "whole document (short)"
            continue
        progress(f"Reading the contents of {d.stem}")
        bookmarks, contents, pmap, has_text = page_map(d.data)
        if not has_text and not bookmarks:
            _whole_or_skip(d, per_doc, warnings, "has no text layer (scanned?), so its contents could not be read")
            continue
        try:
            chosen = select(d, bookmarks, contents, pmap, context, per_doc)
        except Exception as e:
            _whole_or_skip(d, per_doc, warnings, f"page selection failed ({e})")
            continue
        pages: list[int] = []
        sections = []
        for s in chosen:
            new = [p for p in s["pages"] if 1 <= p <= d.page_count and p not in pages]
            if len(pages) + len(new) > per_doc:
                new = new[: max(0, per_doc - len(pages))]
                if not new:
                    warnings.append(f"{d.stem}: page budget reached; '{s['title']}' was not sent.")
                    continue
            pages += new
            sections.append({"title": s["title"], "pages": format_ranges(sorted(new)), "why": s["why"]})
        if not pages:
            _whole_or_skip(d, per_doc, warnings, "no sections relevant to this job were found in its contents")
            continue
        d.how = "chosen automatically from the contents"
        d.pages, d.sections = sorted(pages), sections

    out = []
    for d in docs:
        assert d.pages is not None
        if not d.pages:  # not sent (see warnings)
            continue
        if len(d.pages) < d.page_count:
            name = f"{d.stem} (pages {format_ranges(d.pages)} of {d.page_count}).pdf"
            out.append((name, extract_pages(d.data, d.pages)))
        else:
            out.append((d.name, d.data))
    return out, warnings


def _whole_or_skip(d: RefDoc, per_doc: int, warnings: list[str], reason: str) -> None:
    """When the pages can't be chosen: send a document that fits the budget whole,
    otherwise don't send it (its first pages are front matter, not worth the cost)."""
    if d.page_count <= per_doc:
        d.pages, d.how = list(range(1, d.page_count + 1)), f"whole document ({reason})"
        warnings.append(f"{d.stem}: {reason}; the whole document ({d.page_count} pages) was sent.")
    else:
        d.pages, d.how = [], f"not sent ({reason})"
        warnings.append(f"{d.stem}: {reason}, and at {d.page_count} pages it is too long to send whole, so it was "
                        "NOT used. Enter its 'Pages to review' (e.g. 45-60) and run the review again.")


def parse_selection(text: str, page_count: int) -> list[int]:
    """Claude's 'pdf_pages' string ('45-60, 72') -> page list, clamped to the document."""
    pages: list[int] = []
    text = re.sub(r"\s*(?:-|–|to)\s*", "-", text or "")
    for m in re.finditer(r"(\d+)(?:-(\d+))?", text):
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        a, b = sorted((a, b))
        a, b = max(1, a), min(page_count, b)
        pages += [p for p in range(a, b + 1) if p not in pages]
    return pages


def report(docs: list[RefDoc]) -> list[dict]:
    return [
        {
            "document": d.stem,
            "pages": format_ranges(d.pages or []),
            "page_count": d.page_count,
            "pages_sent": len(d.pages or []),
            "how": d.how,
            "sections": d.sections,
            "store": d.store,
        }
        for d in docs
    ]
