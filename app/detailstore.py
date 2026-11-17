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


# ---------------------------------------------------------------------------
# Splitting a tile into several details (crop tool with suggested boxes)
# ---------------------------------------------------------------------------

_TITLE_RX = re.compile(r"\b(DETAIL|SECTION|ELEVATION|PLAN|TYPICAL)\b[^\n]{0,40}", re.I)


def _page_size(key: str) -> tuple[float, float, str, bytes]:
    got = load(key)
    if got is None:
        raise ValueError("No such detail.")
    _, data, kind = got
    if kind == "pdf":
        with pymupdf.open(stream=data, filetype="pdf") as d:
            r = d[0].rect
            return r.width, r.height, kind, data
    pix = pymupdf.Pixmap(data)
    return float(pix.width), float(pix.height), kind, data


def preview(key: str, longest: int = 1600) -> bytes:
    """A large PNG of the tile for the crop tool."""
    w, h, kind, data = _page_size(key)
    zoom = longest / max(w, h)
    if kind == "pdf":
        with pymupdf.open(stream=data, filetype="pdf") as d:
            return d[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, stream=data)
    return page.get_pixmap(matrix=pymupdf.Matrix(min(zoom, 1.0), min(zoom, 1.0))).tobytes("png")


def _components(grid: list[list[bool]], gap: int) -> list[tuple[int, int, int, int]]:
    """Bounding boxes (c0, r0, c1, r1) of the marked cells, joining marks up to `gap` cells apart."""
    rows, cols = len(grid), len(grid[0])
    dil = [[False] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            if grid[r][c]:
                for rr in range(max(0, r - gap), min(rows, r + gap + 1)):
                    for cc in range(max(0, c - gap), min(cols, c + gap + 1)):
                        dil[rr][cc] = True
    seen = [[False] * cols for _ in range(rows)]
    boxes = []
    for r in range(rows):
        for c in range(cols):
            if not dil[r][c] or seen[r][c]:
                continue
            stack, cells = [(r, c)], []
            seen[r][c] = True
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if 0 <= ny < rows and 0 <= nx < cols and dil[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((ny, nx))
            marked = [(y, x) for y, x in cells if grid[y][x]]
            if marked:
                ys, xs = [y for y, _ in marked], [x for _, x in marked]
                boxes.append((min(xs), min(ys), max(xs) + 1, max(ys) + 1))
    return boxes


def suggest(key: str) -> dict:
    """Suggested boxes round each separate detail on a tile: the drawing's line work and text
    (images: their dark pixels) grouped where they come within ~1.5 % of the page of each other;
    page frames / long border lines are ignored. Returns {"width", "height", "regions": [{x0, y0,
    x1, y1 (0-1), name}]} in reading order."""
    w, h, kind, data = _page_size(key)
    n = 160  # cells along the longer side
    cell = max(w, h) / n
    cols, rows = max(1, int(w / cell) + 1), max(1, int(h / cell) + 1)
    grid = [[False] * cols for _ in range(rows)]
    texts: list[tuple[pymupdf.Rect, str, float]] = []

    def mark(x0, y0, x1, y1):
        for r in range(max(0, int(y0 / cell)), min(rows, int(y1 / cell) + 1)):
            for c in range(max(0, int(x0 / cell)), min(cols, int(x1 / cell) + 1)):
                grid[r][c] = True

    if kind == "pdf":
        with pymupdf.open(stream=data, filetype="pdf") as d:
            page = d[0]
            for dr in page.get_drawings():
                for it in dr["items"]:
                    pts = [p for p in it[1:] if isinstance(p, pymupdf.Point)]
                    rect = it[1] if it[0] == "re" else (pymupdf.Rect(pts[0], pts[0]) if pts else None)
                    if rect is None:
                        continue
                    for p in pts[1:]:
                        rect |= p
                    if rect.width > 0.8 * w or rect.height > 0.8 * h:
                        continue  # page frame / border line
                    mark(rect.x0, rect.y0, rect.x1, rect.y1)
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    t = "".join(s["text"] for s in line["spans"]).strip()
                    if not t:
                        continue
                    r = pymupdf.Rect(line["bbox"])
                    mark(r.x0, r.y0, r.x1, r.y1)
                    texts.append((r, t, max(s["size"] for s in line["spans"])))
            for img in page.get_image_info():
                r = pymupdf.Rect(img["bbox"])
                if r.width < 0.8 * w or r.height < 0.8 * h:
                    mark(r.x0, r.y0, r.x1, r.y1)
    else:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.Pixmap(data))
        step = max(1, int(max(pix.width, pix.height) / (n * 3)))
        samples, stride = pix.samples, pix.stride
        dark_rows = [0] * pix.height
        dark_cols = [0] * pix.width
        hits = []
        for y in range(0, pix.height, step):
            base = y * stride
            for x in range(0, pix.width, step):
                if samples[base + x] < 160:
                    hits.append((x, y))
                    dark_rows[y] += 1
                    dark_cols[x] += 1
        nx, ny = pix.width / step, pix.height / step
        long_rows = {y for y, k in enumerate(dark_rows) if k > 0.6 * nx}
        long_cols = {x for x, k in enumerate(dark_cols) if k > 0.6 * ny}
        for x, y in hits:
            if y in long_rows or x in long_cols:
                continue  # frame lines
            mark(x, y, x + step, y + step)
    boxes = _components(grid, gap=2)
    min_area = 0.004 * cols * rows
    boxes = [b for b in boxes if (b[2] - b[0]) * (b[3] - b[1]) >= min_area and (b[2] - b[0]) > 2 and (b[3] - b[1]) > 2]
    boxes.sort(key=lambda b: (round(b[1] / max(rows / 6, 1)), b[0]))  # rows of details, left to right
    rects = [pymupdf.Rect(max(0.0, (c0 - 1) * cell), max(0.0, (r0 - 1) * cell),
                          min(w, (c1 + 1) * cell), min(h, (r1 + 1) * cell)) for c0, r0, c1, r1 in boxes]
    # A detail's title usually sits just below it: a title line (in its own small box, or in no
    # box) is merged into the box directly above it.
    for tr, t, _ in texts:
        if not _TITLE_RX.search(t):
            continue
        own = next((k for k, r in enumerate(rects) if r.contains(tr)), None)
        if own is not None and rects[own].height > 3 * tr.height + 2 * cell:
            continue  # already inside a detail
        above = [k for k, r in enumerate(rects) if k != own and r.x0 - cell <= (tr.x0 + tr.x1) / 2 <= r.x1 + cell
                 and -cell <= tr.y0 - r.y1 <= 0.08 * h]
        if not above:
            continue
        k = min(above, key=lambda k: tr.y0 - rects[k].y1)
        merged = pymupdf.Rect(rects[k]) | pymupdf.Rect(tr.x0 - cell / 2, tr.y0, tr.x1 + cell / 2, min(h, tr.y1 + cell / 2))
        if own is not None:
            merged |= rects[own]
        rects[k] = merged
        if own is not None:
            rects[own] = pymupdf.Rect()  # absorbed
    rects = [r for r in rects if not r.is_empty]
    regions = []
    for i, box in enumerate(rects, 1):
        x0, y0, x1, y1 = box.x0, box.y0, box.x1, box.y1
        inside = [t for t in texts if box.contains(t[0])]
        titled = [t for t in inside if _TITLE_RX.search(t[1])]
        pick = (titled or sorted(inside, key=lambda t: -t[2]))[:1]
        name = pick[0][1][:60] if pick else f"Detail {i}"
        regions.append({"x0": x0 / w, "y0": y0 / h, "x1": x1 / w, "y1": y1 / h, "name": name})
    return {"width": w, "height": h, "regions": regions}


def split(key: str, regions: list[dict]) -> list[dict]:
    """New tiles cut from a tile, one per region {x0, y0, x1, y1 (0-1), name}: PDF regions stay
    vector (a page clipped to the region), image regions are cropped at full resolution."""
    w, h, kind, data = _page_size(key)
    out = []
    for i, reg in enumerate(regions, 1):
        x0, x1 = sorted((float(reg["x0"]), float(reg["x1"])))
        y0, y1 = sorted((float(reg["y0"]), float(reg["y1"])))
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(1, x1), min(1, y1)
        if (x1 - x0) * w < 10 or (y1 - y0) * h < 10:
            continue
        clip = pymupdf.Rect(x0 * w, y0 * h, x1 * w, y1 * h)
        name = str(reg.get("name") or f"Detail {i}").strip()[:120]
        if kind == "pdf":
            with pymupdf.open(stream=data, filetype="pdf") as src:
                doc = pymupdf.open()
                page = doc.new_page(width=clip.width, height=clip.height)
                page.show_pdf_page(page.rect, src, 0, clip=clip)
                out.append(_store(name, doc.tobytes(garbage=3, deflate=True), "pdf"))
        else:
            doc = pymupdf.open()
            page = doc.new_page(width=w, height=h)
            page.insert_image(page.rect, stream=data)
            png = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(1, 1)).tobytes("png")
            out.append(_store(name, png, "png"))
    return out
