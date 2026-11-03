"""Firm design rules applied to Claude's output after each step.

A prompt asks Claude to follow these rules, but a prompt is not a guarantee, so
the output is checked here as well. Any value that breaks a rule is corrected,
and a warning says exactly what was changed so the engineer can see it.
"""

from __future__ import annotations

import os
import re

from pydantic import BaseModel

MIN_STRIP_WIDTH_MM = int(os.environ.get("MIN_STRIP_FOOTING_WIDTH_MM", "450"))

# Width forms used in footing callouts / schedules, e.g. "300W", "300 WIDE",
# "WIDTH 300", "300x450 DEEP STRIP FOOTING" (width x depth).
_WIDTH_PATTERNS = [
    re.compile(r"\b(\d{3,4})(\s*(?:MM)?\s*)(W)\b", re.I),
    re.compile(r"\b(\d{3,4})(\s*(?:MM)?\s*)(WIDE)\b", re.I),
    re.compile(r"\b(WIDTH\s*(?:OF\s*|=\s*|:\s*)?)(\d{3,4})\b", re.I),
    re.compile(r"\b(\d{3,4})(\s*(?:MM)?\s*[xX×]\s*(?:MIN\.?\s*)?\d{3,4}\s*(?:MM)?\s*(?:DEEP\s*)?STRIP)", re.I),
]
_SEGMENT_SPLIT = re.compile(r"(?<=[.;\n])\s+")


def _fix_segment(seg: str) -> tuple[str, list[int]]:
    raised: list[int] = []

    def bump(value: str) -> str:
        v = int(value)
        if 100 <= v < MIN_STRIP_WIDTH_MM:
            raised.append(v)
            return str(MIN_STRIP_WIDTH_MM)
        return value

    for pat in _WIDTH_PATTERNS:
        if pat.pattern.startswith(r"\b(WIDTH"):
            seg = pat.sub(lambda m: m.group(1) + bump(m.group(2)), seg)
        else:
            seg = pat.sub(lambda m: bump(m.group(1)) + "".join(g or "" for g in m.groups()[1:]), seg)
    return seg, raised


def enforce_strip_width(text: str, force: bool = False) -> tuple[str, list[int]]:
    """Raise strip footing widths below the minimum. Only sentences that mention a
    strip footing are touched (or every sentence when force=True, e.g. an SF tag),
    so internal beams, piers, bolts etc. are left alone."""
    if not text:
        return text, []
    parts = _SEGMENT_SPLIT.split(text)
    seps = _SEGMENT_SPLIT.findall(text)
    raised: list[int] = []
    out = []
    for seg in parts:
        if force or re.search(r"\bSTRIP", seg, re.I):
            seg, r = _fix_segment(seg)
            raised += r
        out.append(seg)
    rebuilt = out[0] + "".join(sep + seg for sep, seg in zip(seps, out[1:]))
    return rebuilt, raised


def _is_strip_tag(tag: str) -> bool:
    return re.match(r"\s*SF\d*\b", tag or "", re.I) is not None


def apply_strip_rule(model: BaseModel, where: str) -> list[str]:
    """Walk every text field in a Claude output model, correcting strip footing
    widths in place. Returns human-readable warnings for each change."""
    warnings: list[str] = []

    def note(raised: list[int], before: str) -> None:
        for v in raised:
            snippet = before.strip().replace("\n", " ")
            snippet = snippet[:70] + ("..." if len(snippet) > 70 else "")
            warnings.append(
                f"{where}: strip footing width raised from {v} to {MIN_STRIP_WIDTH_MM} mm minimum ('{snippet}')"
            )

    def walk(m: BaseModel, force: bool = False) -> None:
        force = force or _is_strip_tag(getattr(m, "tag", ""))
        for name in type(m).model_fields:
            val = getattr(m, name)
            if isinstance(val, str):
                new, raised = enforce_strip_width(val, force)
                if raised:
                    setattr(m, name, new)
                    note(raised, val)
            elif isinstance(val, BaseModel):
                walk(val, force)
            elif isinstance(val, list):
                for i, item in enumerate(val):
                    if isinstance(item, str):
                        new, raised = enforce_strip_width(item, force)
                        if raised:
                            val[i] = new
                            note(raised, item)
                    elif isinstance(item, BaseModel):
                        walk(item, force)

    walk(model)
    return warnings


def check_geotech_strip(texts: list[str]) -> list[str]:
    """The geotech report is quoted as written (not altered); just flag any strip
    footing narrower than the minimum so the engineer sees the conflict."""
    out = []
    for t in texts:
        _, raised = enforce_strip_width(t)
        for v in raised:
            out.append(
                f"Geotech report: recommends a {v} mm wide strip footing; mark-ups use the "
                f"{MIN_STRIP_WIDTH_MM} mm minimum ('{t.strip()[:70]}')"
            )
    return out
