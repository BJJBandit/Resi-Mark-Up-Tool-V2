"""Firm design rules (office rules) applied to Claude's output after each step.

A prompt asks Claude to follow these rules, but a prompt is not a guarantee, so
the output is checked here as well. Any value that breaks a rule is corrected,
and a warning says exactly what was changed so the engineer can see it.

Rules:
- Strip footings are at least MIN_STRIP_WIDTH_MM (450) wide.
- Edge / internal / stiffening beams that AS 2870 gives as 300 wide are made
  450 wide (300 + 150).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

MIN_STRIP_WIDTH_MM = int(os.environ.get("MIN_STRIP_FOOTING_WIDTH_MM", "450"))
BEAM_WIDTH_FROM_MM = int(os.environ.get("BEAM_WIDTH_FROM_MM", "300"))
BEAM_WIDTH_TO_MM = int(os.environ.get("BEAM_WIDTH_TO_MM", "450"))

_SEGMENT_SPLIT = re.compile(r"(?<=[.;\n])\s+")


@dataclass
class WidthRule:
    name: str  # e.g. "strip footing"
    keyword: str  # regex: a sentence mentioning this gets checked
    tag: str  # regex: schedule tags whose whole text gets checked
    new_width: Callable[[int], int | None]  # corrected width, or None if fine

    def patterns(self, forced: bool = False) -> list[re.Pattern]:
        """Width forms used in callouts / schedules: '300W', '300 WIDE', 'WIDTH 300',
        '300x450 DEEP <member>' (width x depth). In a schedule item already tagged as
        this member (forced), a leading '300x500 ...' is width x depth too."""
        extra = [re.compile(r"^(\s*\d{3,4})(\s*(?:MM)?\s*[xX\u00d7]\s*(?:MIN\.?\s*)?\d{3,4})", re.I)] if forced else []
        return extra + [
            re.compile(r"\b(\d{3,4})(\s*(?:MM)?\s*)(W)\b", re.I),
            re.compile(r"\b(\d{3,4})(\s*(?:MM)?\s*)(WIDE)\b", re.I),
            re.compile(r"\b(WIDTH\s*(?:OF\s*|=\s*|:\s*)?)(\d{3,4})\b", re.I),
            re.compile(r"\b(\d{3,4})(\s*(?:MM)?\s*[xX×]\s*(?:MIN\.?\s*)?\d{3,4}\s*(?:MM)?\s*(?:DEEP\s*)?(?:"
                       + self.keyword + r"))", re.I),
        ]


STRIP_RULE = WidthRule(
    "strip footing", r"\bSTRIP", r"SF\d*\b",
    lambda v: MIN_STRIP_WIDTH_MM if 100 <= v < MIN_STRIP_WIDTH_MM else None,
)
BEAM_RULE = WidthRule(
    "edge / internal beam", r"\b(?:EDGE|INTERNAL|STIFFEN\w*)\s+BEAMS?\b", r"(?:EB|IB)\d*\b",
    lambda v: BEAM_WIDTH_TO_MM if v == BEAM_WIDTH_FROM_MM else None,
)
RULES = [BEAM_RULE, STRIP_RULE]


def _fix_segment(seg: str, rule: WidthRule, forced: bool = False) -> tuple[str, list[tuple[int, int]]]:
    changed: list[tuple[int, int]] = []

    def fix(value: str) -> str:
        lead = value[: len(value) - len(value.lstrip())]
        new = rule.new_width(int(value))
        if new is None:
            return value
        changed.append((int(value), new))
        return lead + str(new)

    for pat in rule.patterns(forced):
        if pat.pattern.startswith(r"\b(WIDTH"):
            seg = pat.sub(lambda m: m.group(1) + fix(m.group(2)), seg)
        else:
            seg = pat.sub(lambda m: fix(m.group(1)) + "".join(g or "" for g in m.groups()[1:]), seg)
    return seg, changed


def enforce(text: str, rule: WidthRule, force: bool = False) -> tuple[str, list[tuple[int, int]]]:
    """Apply one width rule to a text. Only sentences that mention the member are
    touched (or every sentence when force=True, e.g. a schedule item tagged SF / EB),
    so other members, piers, depths, bar spacings etc. are left alone."""
    if not text:
        return text, []
    parts = _SEGMENT_SPLIT.split(text)
    seps = _SEGMENT_SPLIT.findall(text)
    changed: list[tuple[int, int]] = []
    out = []
    for seg in parts:
        if force or re.search(rule.keyword, seg, re.I):
            seg, c = _fix_segment(seg, rule, force)
            changed += c
        out.append(seg)
    return out[0] + "".join(sep + seg for sep, seg in zip(seps, out[1:])), changed


def enforce_strip_width(text: str, force: bool = False) -> tuple[str, list[int]]:
    """Back-compatible: strip footing rule only; returns the widths that were raised."""
    new, changed = enforce(text, STRIP_RULE, force)
    return new, [v for v, _ in changed]


def apply_rules(model: BaseModel, where: str) -> list[str]:
    """Walk every text field in a Claude output model, applying every rule in
    place. Returns a warning per change."""
    warnings: list[str] = []

    def note(rule: WidthRule, changed: list[tuple[int, int]], before: str) -> None:
        snippet = before.strip().replace("\n", " ")
        snippet = snippet[:70] + ("..." if len(snippet) > 70 else "")
        for old, new in changed:
            warnings.append(f"{where}: {rule.name} width changed from {old} to {new} mm (office rule) ('{snippet}')")

    def fix(text: str, forced: set[str]) -> str:
        for rule in RULES:
            new, changed = enforce(text, rule, rule.name in forced)
            if changed:
                note(rule, changed, text)
                text = new
        return text

    def walk(m: BaseModel, forced: set[str]) -> None:
        tag = getattr(m, "tag", "") or ""
        forced = forced | {r.name for r in RULES if re.match(r"\s*" + r.tag, tag, re.I)}
        for name in type(m).model_fields:
            val = getattr(m, name)
            if isinstance(val, str):
                new = fix(val, forced)
                if new != val:
                    setattr(m, name, new)
            elif isinstance(val, BaseModel):
                walk(val, forced)
            elif isinstance(val, list):
                for i, item in enumerate(val):
                    if isinstance(item, str):
                        new = fix(item, forced)
                        if new != item:
                            val[i] = new
                    elif isinstance(item, BaseModel):
                        walk(item, forced)

    walk(model, set())
    return warnings


def check_geotech(texts: list[str]) -> list[str]:
    """The geotech report is quoted as written (not altered); flag anything that
    conflicts with an office rule so the engineer sees it."""
    out = []
    for t in texts:
        for rule in RULES:
            _, changed = enforce(t, rule)
            for old, new in changed:
                out.append(f"Geotech report: gives a {old} mm wide {rule.name}; mark-ups use {new} mm "
                           f"(office rule) ('{t.strip()[:70]}')")
    return out


# Back-compatible names
apply_strip_rule = apply_rules
check_geotech_strip = check_geotech
