"""Model choice per step, and what each review actually cost.

Steps 1-2 (geotech + architectural review) make the engineering calls, so they
always use the most capable model. Once those have reviewed the documents, each
mark-up sheet is routed to the cheapest model that suits it: Claude Sonnet 5.5
(half the price of Opus) for straightforward sheets, Claude Opus 5.5 where the
sheet needs judgement. Claude Haiku 5.5 is not used: it is built for simple
high-volume tasks rather than reading structural drawings, and has no server-side
fallback when it declines a request.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field

from .schemas import SHEET_TITLES, DesignAssessment, GeotechReview, SheetKind

OPUS = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
SONNET = os.environ.get("ANTHROPIC_SHEET_MODEL_SIMPLE", "claude-sonnet-5-5")
ROUTING = os.environ.get("SHEET_MODEL_ROUTING", "auto")  # auto | <model id>

LABELS = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-opus-5": "Claude Opus 5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-fable-5-1": "Claude Fable 5.1",
}

# USD per million tokens: (input, output, cache read, 5-min cache write).
# Published Claude API list prices; check https://www.anthropic.com/pricing if budgeting.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50),
}


def label(model: str) -> str:
    return LABELS.get(model, model)


# ---------------------------------------------------------------------------
# Cost ledger
# ---------------------------------------------------------------------------


@dataclass
class Call:
    step: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    cost: float


@dataclass
class Ledger:
    calls: list[Call] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, step: str, model: str, usage) -> None:
        g = lambda name: int(getattr(usage, name, 0) or 0)  # noqa: E731
        inp, out = g("input_tokens"), g("output_tokens")
        cr, cw = g("cache_read_input_tokens"), g("cache_creation_input_tokens")
        p_in, p_out, p_cr, p_cw = PRICES.get(model, PRICES["claude-opus-5-5"])
        cost = (inp * p_in + out * p_out + cr * p_cr + cw * p_cw) / 1e6
        with self._lock:
            self.calls.append(Call(step, model, inp, out, cr, cw, cost))

    def total(self) -> float:
        return sum(c.cost for c in self.calls)

    def by_model(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for c in self.calls:
            out[label(c.model)] = out.get(label(c.model), 0.0) + c.cost
        return out

    def summary(self) -> dict:
        return {
            "total_usd": round(self.total(), 4),
            "by_model_usd": {k: round(v, 4) for k, v in self.by_model().items()},
            "calls": [
                {"step": c.step, "model": c.model, "input_tokens": c.input_tokens,
                 "output_tokens": c.output_tokens, "cache_read_tokens": c.cache_read,
                 "cache_write_tokens": c.cache_write, "cost_usd": round(c.cost, 4)}
                for c in self.calls
            ],
        }


# ---------------------------------------------------------------------------
# Model per mark-up sheet, chosen after the documents are reviewed
# ---------------------------------------------------------------------------

REACTIVE = {"H1", "H1-D", "H2", "H2-D", "E", "E-D", "P"}
HIGH_WIND = re.compile(r"\b(N[4-6]|C[1-4])\b", re.I)


def _fill_present(text: str) -> bool:
    t = (text or "").strip().lower()
    return bool(t) and not (t in {"not stated", "none", "nil", "no", "n/a", "-"} or t.startswith(("no ", "nil ", "none ")))


def choose_sheet_models(
    geotech: GeotechReview, design: DesignAssessment, override: str = ""
) -> dict[SheetKind, tuple[str, str]]:
    """Returns {sheet: (model id, reason)}."""
    choice = (override or ROUTING or "auto").strip()
    if choice and choice != "auto":
        return {k: (choice, "chosen on the form") for k in SheetKind}

    site = (design.footing_site_class_used or geotech.site_classification or "").upper().strip()
    complex_job = design.complexity.strip().lower() == "complex"
    two_storey = design.storeys >= 2
    non_timber = not re.search(r"^\s*timber\s*$", design.framing_material or "", re.I)
    high_wind = bool(HIGH_WIND.search(f"{design.wind_classification_used} {geotech.wind_classification}"))
    reactive = site in REACTIVE
    fill = _fill_present(geotech.fill)
    suspended_slab = "suspended slab" in (design.floor_system or "").lower()

    def pick(*reasons: tuple[bool, str]) -> tuple[str, str]:
        hits = [r for flag, r in reasons if flag]
        if hits:
            return OPUS, "; ".join(hits)
        return SONNET, "straightforward sheet"

    cx = (complex_job, f"rated complex in review ({design.complexity_reason.strip()[:80]})")
    return {
        SheetKind.LOAD_PATHS: pick(cx, (two_storey, "two or more storeys")),
        SheetKind.ROOF: pick(cx, (high_wind, "high wind class")),
        SheetKind.WALLS: pick(cx, (two_storey, "two or more storeys"), (non_timber, f"{design.framing_material} framing"),
                              (high_wind, "high wind class")),
        SheetKind.FOOTINGS: pick(cx, (reactive, f"site class {site}"), (fill, "fill on site")),
        SheetKind.FLOOR: pick(cx, (suspended_slab, "suspended slab"), (reactive, f"site class {site}")),
    }


def describe(models: dict[SheetKind, tuple[str, str]]) -> dict[str, str]:
    return {SHEET_TITLES[k]: f"{label(m)} - {why}" for k, (m, why) in models.items()}
