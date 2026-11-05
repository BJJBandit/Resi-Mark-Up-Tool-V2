"""Stored text extracts of standards, guides and codes, so each page is read once.

The first time a page of a reference document is needed, Claude transcribes it
(clauses, tables row by row, notes, what each figure shows) and the text is saved
on disk, keyed by the document's content hash and PDF page number. Every later
step and every later job sends the stored text instead of the PDF page, so a page
is only ever read from the PDF once - whatever the file is called and whoever
uploads it. Pages that can't be transcribed are sent as PDF pages as before.

Set REF_STORE_DIR to a persistent disk (see render.yaml) so the store survives
redeploys. REF_MODE=pdf turns the store off and sends PDF pages every time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

STORE_DIR = Path(os.environ.get("REF_STORE_DIR", "data/references"))
MODE = os.environ.get("REF_MODE", "digest").strip().lower()  # digest | pdf
CHUNK = 6  # pages per transcription call
WORKERS = 4

_lock = threading.Lock()


def enabled() -> bool:
    return MODE != "pdf"


def doc_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _dir(key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise ValueError("bad document key")
    return STORE_DIR / key


def _page_file(key: str, page: int) -> Path:
    return _dir(key) / f"p{page:04d}.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)  # atomic: a reader never sees half a page


def _save_meta(key: str, name: str, page_count: int) -> None:
    path = _dir(key) / "meta.json"
    with _lock:
        meta = json.loads(path.read_text()) if path.exists() else {}
        names = sorted(set(meta.get("names", [])) | {name})
        _write(path, json.dumps({"names": names, "page_count": page_count}))


def stored_page(key: str, page: int) -> str | None:
    f = _page_file(key, page)
    return f.read_text(encoding="utf-8") if f.exists() else None


def get_text(
    key: str,
    name: str,
    page_count: int,
    pages: list[int],
    transcribe: Callable[[list[int]], dict[int, str]],
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[dict[int, str], int, list[int]]:
    """Text of each requested page: from the store, or transcribed now and stored.

    transcribe(pages) -> {page: text} for one chunk of the document.
    Returns ({page: text}, pages newly transcribed, pages that failed)."""
    _save_meta(key, name, page_count)
    have = {p: t for p in pages if (t := stored_page(key, p)) is not None}
    missing = [p for p in pages if p not in have]
    if not missing:
        return have, 0, []
    chunks = [missing[i:i + CHUNK] for i in range(0, len(missing), CHUNK)]
    done = [0]
    failed: list[int] = []

    def run(chunk: list[int]) -> None:
        try:
            got = transcribe(chunk)
        except Exception:
            got = {}
        with _lock:
            for p in chunk:
                text = (got.get(p) or "").strip()
                if text:
                    _write(_page_file(key, p), text)
                    have[p] = text
                else:
                    failed.append(p)
            done[0] += 1
            progress(f"Extracting {name}: {done[0]} of {len(chunks)} part(s) "
                     f"({len(missing)} new page(s); {len(pages) - len(missing)} already stored)")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(run, chunks))
    return have, len(missing) - len(failed), sorted(failed)


def as_text(name: str, texts: dict[int, str]) -> str:
    return "\n\n".join(f"===== {name} - PDF page {p} =====\n{texts[p]}" for p in sorted(texts))


def list_documents() -> list[dict]:
    out = []
    if not STORE_DIR.exists():
        return out
    for d in sorted(STORE_DIR.iterdir()):
        meta_f = d / "meta.json"
        if not meta_f.exists():
            continue
        meta = json.loads(meta_f.read_text())
        pages = sorted(int(f.stem[1:]) for f in d.glob("p*.md"))
        out.append({"key": d.name, "names": meta.get("names", []), "page_count": meta.get("page_count"),
                    "pages_stored": len(pages), "pages": pages})
    return out


def document_text(key: str) -> str | None:
    d = _dir(key)
    if not (d / "meta.json").exists():
        return None
    meta = json.loads((d / "meta.json").read_text())
    name = (meta.get("names") or ["document"])[0]
    texts = {int(f.stem[1:]): f.read_text(encoding="utf-8") for f in sorted(d.glob("p*.md"))}
    return as_text(name, texts)


def delete_document(key: str) -> bool:
    d = _dir(key)
    if not d.exists():
        return False
    for f in d.iterdir():
        f.unlink()
    d.rmdir()
    return True
