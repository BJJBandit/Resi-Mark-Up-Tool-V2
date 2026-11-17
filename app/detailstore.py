"""Stored standard details (images or PDF pages) for the DETAILS sheet.

Uploaded once from the Details panel and kept on disk (DETAILS_DIR - a persistent disk on
Render), keyed by content hash. Each detail keeps its original file (PNG / JPG, or a PDF -
its first page is used), a PNG thumbnail for the panel and its name."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import pymupdf

DETAILS_DIR = Path(os.environ.get("DETAILS_DIR", "data/details"))
MAX_BYTES = 20 * 1024 * 1024
_lock = threading.Lock()


def _key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:24]


def _dir(key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{24}", key or ""):
        raise ValueError("bad detail id")
    return DETAILS_DIR / key


def kind_of(data: bytes) -> str | None:
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    return None


def _thumb(data: bytes, kind: str) -> bytes:
    if kind == "pdf":
        with pymupdf.open(stream=data, filetype="pdf") as d:
            page = d[0]
            zoom = 360 / max(page.rect.width, page.rect.height)
            return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
    pix = pymupdf.Pixmap(data)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    scale = 360 / max(pix.width, pix.height)
    if scale < 1:
        doc = pymupdf.open()
        page = doc.new_page(width=pix.width * scale, height=pix.height * scale)
        page.insert_image(page.rect, pixmap=pix)
        return page.get_pixmap().tobytes("png")
    return pix.tobytes("png")


def save(name: str, data: bytes) -> dict:
    """Store a detail (dedupes by content). Returns its listing entry."""
    kind = kind_of(data)
    if kind is None:
        raise ValueError(f"'{name}' is not a PNG, JPG or PDF")
    if len(data) > MAX_BYTES:
        raise ValueError(f"'{name}' is larger than {MAX_BYTES // 1048576} MB")
    key = _key(data)
    d = _dir(key)
    with _lock:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"source.{kind}").write_bytes(data)
        (d / "thumb.png").write_bytes(_thumb(data, kind))
        meta = {"name": re.sub(r"\.(pdf|png|jpe?g)$", "", name or "detail", flags=re.I).strip() or "detail",
                "kind": kind, "added": time.time()}
        if (d / "meta.json").exists():  # same file uploaded again: keep its name
            meta = json.loads((d / "meta.json").read_text())
        (d / "meta.json").write_text(json.dumps(meta))
    return {"id": key, **meta}


def list_details() -> list[dict]:
    out = []
    if not DETAILS_DIR.is_dir():
        return out
    for d in DETAILS_DIR.iterdir():
        try:
            if re.fullmatch(r"[0-9a-f]{24}", d.name) and (d / "meta.json").is_file():
                out.append({"id": d.name, **json.loads((d / "meta.json").read_text())})
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda x: (x.get("name", "").lower(), x.get("added", 0)))


def thumb(key: str) -> bytes | None:
    p = _dir(key) / "thumb.png"
    return p.read_bytes() if p.is_file() else None


def load(key: str) -> tuple[str, bytes, str] | None:
    """(name, file bytes, kind) or None."""
    d = _dir(key)
    if not (d / "meta.json").is_file():
        return None
    meta = json.loads((d / "meta.json").read_text())
    src = d / f"source.{meta['kind']}"
    return (meta["name"], src.read_bytes(), meta["kind"]) if src.is_file() else None


def rename(key: str, name: str) -> None:
    d = _dir(key)
    meta = json.loads((d / "meta.json").read_text())
    meta["name"] = name.strip()[:120] or meta["name"]
    (d / "meta.json").write_text(json.dumps(meta))


def delete(key: str) -> bool:
    d = _dir(key)
    if not d.is_dir():
        return False
    with _lock:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
    return True
