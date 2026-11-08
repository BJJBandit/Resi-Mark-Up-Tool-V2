"""The office stock list of member sizes (the '## Member sizes' section of
office_rules.md) and a check that schedule items only use those sizes."""

from __future__ import annotations

import re
from pathlib import Path

_RULES = Path(__file__).parent / "office_rules.md"


def _norm(size: str) -> str:
    return re.sub(r"\s+", "", size.upper().replace("×", "X"))


def load() -> dict[str, set[str]]:
    """{category: {sizes}} from office_rules.md, e.g. 'F17' -> {'90X35', '2/240X45', ...}."""
    text = _RULES.read_text()
    if "## Member sizes" not in text:
        return {}
    section = text.split("## Member sizes", 1)[1].split("\n## ", 1)[0]
    out: dict[str, set[str]] = {}
    for line in section.splitlines():
        m = re.match(r"-\s*([^:]+):\s*(.+)", line.strip())
        if not m:
            continue
        head = m.group(1).upper()
        key = ("F17" if "F17" in head or "LVL" in head else "F7" if "F7" in head or "TREATED" in head
               else "MGP10" if "MGP" in head else "SHS" if "SHS" in head else "CHS" if "CHS" in head
               else "RHS" if "RHS" in head else "PFC" if "PFC" in head else "UB" if "UB" in head
               else "ANGLE" if "ANGLE" in head or "FLAT" in head else "PURLIN" if "PURLIN" in head else head)
        items = re.split(r"[,;]\s*", m.group(2).rstrip(". "))
        out.setdefault(key, set()).update(_norm(re.sub(r"\s*(PFC|FL|UA|TP POST)$", r" \1", i)) for i in items if i)
    return out


_TIMBER = re.compile(r"(?<![\d.])((?:\d/)?\d{2,3}\s*[xX×]\s*\d{2})(?![\d.]|\s*[xX×])")
_STEEL = [
    ("SHS", re.compile(r"(\d+(?:\.\d+)?\s*[xX]\s*\d+(?:\.\d+)?)\s*SHS", re.I)),
    ("CHS", re.compile(r"(\d+(?:\.\d+)?\s*[xX]\s*\d+(?:\.\d+)?)\s*CHS", re.I)),
    ("RHS", re.compile(r"(\d+\s*[xX]\s*\d+\s*[xX]\s*\d+(?:\.\d+)?)\s*RHS", re.I)),
    ("PFC", re.compile(r"(\d{3}\s*PFC)", re.I)),
    ("UB", re.compile(r"(\d{3}\s*UB\s*\d+(?:\.\d+)?)", re.I)),
    ("ANGLE", re.compile(r"(\d+\s*[xX]\s*\d+\s*[xX]\s*\d+\s*UA|\d+\s*[xX]\s*\d+\s*FL)\b", re.I)),
]


def check(schedule, title: str) -> list[str]:
    """Warnings for schedule items whose timber / steel size isn't on the stock list."""
    stock = load()
    if not stock:
        return []
    bad = []
    for item in schedule:
        text = item.text.upper()
        found = []
        for key, rx in _STEEL:
            for m in rx.finditer(text):
                size = _norm(m.group(1))
                if key == "ANGLE":
                    size = re.sub(r"(UA|FL)$", r"\1", size)
                if size not in {_norm(x) for x in stock.get(key, set())}:
                    found.append(m.group(1).upper())
        grade = ("F17" if re.search(r"F17|LVL", text) else "F7" if re.search(r"\bF7\b|\bTP\b|TREATED", text)
                 else "MGP10" if "MGP" in text else None)
        label = (re.search(r"MGP\d+", text).group(0) if grade == "MGP10" and re.search(r"MGP\d+", text) else grade)
        if grade and not re.search(r"SHS|CHS|RHS|PFC|UB\d|\bUA\b|\bFL\b", text):
            for m in _TIMBER.finditer(text):
                if re.search(r"\d\s*W\b|DEEP|THICK|PLATE", text[m.end():m.end() + 8]):
                    continue  # footing / plate dimensions, not a timber section
                size = _norm(m.group(1))
                if size not in stock.get(grade, set()) or (grade == "MGP10" and label != "MGP10"):
                    found.append(f"{m.group(1).upper()} {label}")
        if found:
            bad.append(f"{item.tag} ({', '.join(dict.fromkeys(found))})")
    if not bad:
        return []
    return [f"{title}: size(s) not on the office stock list - use a stock size or confirm: {'; '.join(bad)}"]
