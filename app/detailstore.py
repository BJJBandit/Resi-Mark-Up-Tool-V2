"""Stored standard details for the DETAILS sheet - one tile per detail.

Uploaded from the Details panel and kept on disk (DETAILS_DIR - a persistent disk on Render),
keyed by content hash. PNG / JPG images are a tile each; every page of a PDF is its own tile; a
DXF drawing is drawn to a vector PDF page. DWG can't be read (closed format) - export DXF / PDF.
Each tile keeps its file, a PNG thumbnail for the panel and its name."""

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


def kind_of(data: bytes, name: str = "") -> str | None:
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"AC10":
        return "dwg"
    head = data[:2048].lstrip().decode("latin-1", "replace").upper()
    if name.lower().endswith(".dxf") or head.startswith("0\r\nSECTION") or head.startswith("0\nSECTION"):
        return "dxf"
    return None


def _dxf_to_pdf(data: bytes) -> bytes:
    """A DXF drawing (model space) as a one-page vector PDF, cropped to the drawing."""
    import io

    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, layout
    from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
    try:
        doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    except UnicodeDecodeError:
        doc = ezdxf.read(io.StringIO(data.decode("cp1252", "replace")))
    backend = PyMuPdfBackend()
    Frontend(RenderContext(doc), backend).draw_layout(doc.modelspace())
    return backend.get_pdf_bytes(layout.Page(0, 0, layout.Units.mm, margins=layout.Margins.all(5)))


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


def _store(name: str, data: bytes, kind: str) -> dict:
    key = _key(data)
    d = _dir(key)
    with _lock:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"source.{kind}").write_bytes(data)
        (d / "thumb.png").write_bytes(_thumb(data, kind))
        meta = {"name": name.strip()[:120] or "detail", "kind": kind, "added": time.time()}
        if (d / "meta.json").exists():  # the same detail uploaded again: keep its name
            meta = json.loads((d / "meta.json").read_text())
        (d / "meta.json").write_text(json.dumps(meta))
    return {"id": key, **meta}


def save(name: str, data: bytes) -> list[dict]:
    """Store a file's details - one tile per detail: each page of a PDF is its own tile, a DXF
    is drawn to a vector page, an image is one tile. Dedupes by content. Returns the entries."""
    kind = kind_of(data, name or "")
    stem = re.sub(r"\.(pdf|png|jpe?g|dxf|dwg)$", "", name or "detail", flags=re.I).strip() or "detail"
    if kind == "dwg":
        raise ValueError(f"'{name}' is a DWG - it can't be read here. In CAD, save it as DXF (SAVEAS > DXF) "
                         "or plot it to PDF, and add that.")
    if kind is None:
        raise ValueError(f"'{name}' is not a PNG, JPG, PDF or DXF")
    if len(data) > MAX_BYTES:
        raise ValueError(f"'{name}' is larger than {MAX_BYTES // 1048576} MB")
    if kind == "dxf":
        try:
            data, kind = _dxf_to_pdf(data), "pdf"
        except Exception as e:
            raise ValueError(f"'{name}' could not be read as a DXF drawing ({e})")
    if kind != "pdf":
        return [_store(stem, data, kind)]
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.page_count == 1:
            return [_store(stem, data, "pdf")]
        out = []
        for i in range(doc.page_count):  # one tile per page
            one = pymupdf.open()
            one.insert_pdf(doc, from_page=i, to_page=i)
            out.append(_store(f"{stem} - {i + 1}", one.tobytes(garbage=3, deflate=True), "pdf"))
        return out


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
