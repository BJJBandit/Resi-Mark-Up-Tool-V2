"""Send Claude only the pages of each standard / guide that the review needs.

For a document whose 'Pages to review' box is left blank, read its bookmarks,
its contents pages and the first lines of every page (the 'page map', which
shows where each section really starts even when printed page numbers differ
from PDF page numbers), ask Claude which sections this job needs, and extract
just those pages. Pages typed on the form are used as given; 'all' sends the
whole document. Every document's pages are reported back to the engineer.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from .pdf import extract_pages, format_ranges

# Pages of standards / guides sent per review, across all documents. Kept low for beta
# to control cost (REF_PAGE_CAP); never above 500 - Claude reads at most 600 PDF pages
# per request and the drawings need room too.
MAX_REF_PAGES = max(10, min(500, int(os.environ.get("REF_PAGE_CAP", "150"))))
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

    # 1. Short documents go whole (they are cheap and need no index).
    for d in auto:
        if d.page_count <= SMALL_DOC_PAGES and d.page_count <= remaining:
            d.pages, d.how = list(range(1, d.page_count + 1)), "whole document (short)"
            remaining -= d.page_count

    # 2. Claude picks the sections each long document needs (told the whole budget left).
    picks: dict[int, list[dict]] = {}
    fallback: list[tuple[RefDoc, str]] = []
    for d in auto:
        if d.pages is not None:
            continue
        progress(f"Reading the contents of {d.stem}")
        bookmarks, contents, pmap, has_text = page_map(d.data)
        if not has_text and not bookmarks:
            fallback.append((d, "has no text layer (scanned?), so its contents could not be read"))
            continue
        try:
            chosen = select(d, bookmarks, contents, pmap, context, max(10, remaining))
        except Exception as e:
            fallback.append((d, f"page selection failed ({e})"))
            continue
        secs = []
        seen: set[int] = set()
        for sec in chosen:
            new = [p for p in sec["pages"] if 1 <= p <= d.page_count and p not in seen]
            seen.update(new)
            if new:
                secs.append({**sec, "pages": new})
        if secs:
            picks[id(d)] = secs
        else:
            fallback.append((d, "no sections relevant to this job were found in its contents"))

    # 3. Fit the sections into the budget, one section per document in turn (each
    #    document's sections in Claude's order of need), so unused budget isn't wasted
    #    and no single document crowds out the others.
    queues = {k: list(v) for k, v in picks.items()}
    taken: dict[int, list[dict]] = {k: [] for k in picks}
    while any(queues.values()):
        for k in list(queues):
            if not queues[k]:
                continue
            sec = queues[k].pop(0)
            if len(sec["pages"]) <= remaining:
                taken[k].append(sec)
                remaining -= len(sec["pages"])
            else:
                doc = next(d for d in auto if id(d) == k)
                warnings.append(f"{doc.stem}: page limit ({budget_total} per review) reached; "
                                f"'{sec['title']}' was not sent.")
    for d in auto:
        if id(d) not in picks:
            continue
        secs = taken[id(d)]
        if not secs:
            d.pages, d.how = [], "not sent (page limit reached)"
            continue
        d.how = "chosen automatically from the contents"
        d.pages = sorted(p for sec in secs for p in sec["pages"])
        d.sections = [{"title": sec["title"], "pages": format_ranges(sorted(sec["pages"])), "why": sec["why"]}
                      for sec in secs]

    # 4. Documents whose pages couldn't be chosen: whole if they fit what's left.
    for d, reason in fallback:
        _whole_or_skip(d, remaining, warnings, reason)
        remaining -= len(d.pages or [])
    for d in auto:  # short documents that didn't fit
        if d.pages is None:
            d.pages, d.how = [], "not sent (page limit reached)"
            warnings.append(f"{d.stem}: page limit ({budget_total} per review) reached, so it was not sent.")

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


def _whole_or_skip(d: RefDoc, budget: int, warnings: list[str], reason: str) -> None:
    """When the pages can't be chosen: send a document that fits the budget whole,
    otherwise don't send it (its first pages are front matter, not worth the cost)."""
    if d.page_count <= budget:
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
