"""Finished reviews saved to disk, so their mark-up PDF and results can still be opened
after the service restarts or redeploys (reviews in memory are lost then).

Each review is a folder <JOBS_DIR>/<job id>/ holding status.json (what the results page
shows), review.json and markup.pdf. Only the newest JOBS_KEEP are kept. A review loaded
from disk can be viewed and downloaded but not re-run (its drawings aren't stored)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

JOBS_DIR = Path(os.environ.get("JOBS_DIR", "data/jobs"))
JOBS_KEEP = int(os.environ.get("JOBS_KEEP", "40"))


def _dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("bad job id")
    return JOBS_DIR / job_id


def save(job_id: str, project: str, status: dict, review: dict, pdf: bytes) -> None:
    d = _dir(job_id)
    tmp = d.with_name(d.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "status.json").write_text(json.dumps({**status, "project": project, "saved_at": time.time()}))
    (tmp / "review.json").write_text(json.dumps(review))
    (tmp / "markup.pdf").write_bytes(pdf)
    shutil.rmtree(d, ignore_errors=True)
    tmp.replace(d)  # a reader sees the whole review or none of it
    _prune()


def _folders() -> list[Path]:
    if not JOBS_DIR.is_dir():
        return []
    ds = [d for d in JOBS_DIR.iterdir() if re.fullmatch(r"[0-9a-f]{32}", d.name) and (d / "status.json").is_file()]
    return sorted(ds, key=lambda d: (d / "status.json").stat().st_mtime, reverse=True)


def _prune() -> None:
    for d in _folders()[JOBS_KEEP:]:
        shutil.rmtree(d, ignore_errors=True)


def load_status(job_id: str) -> dict | None:
    try:
        f = _dir(job_id) / "status.json"
        return json.loads(f.read_text()) if f.is_file() else None
    except (ValueError, OSError, json.JSONDecodeError):
        return None


def load_file(job_id: str, name: str) -> bytes | None:
    try:
        f = _dir(job_id) / name
        return f.read_bytes() if f.is_file() else None
    except (ValueError, OSError):
        return None


def list_saved() -> list[dict]:
    out = []
    for d in _folders():
        try:
            s = json.loads((d / "status.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": d.name, "project": s.get("project") or "(no address)", "saved_at": s.get("saved_at"),
                    "sheets": [r["title"] for r in s.get("rerunnable", []) if r.get("made")],
                    "cost_usd": (s.get("usage") or {}).get("cost_usd")})
    return out
