"""Token usage and estimated cost of every Claude call, per review.

Each review gets a UsageLog (set in a context variable for the thread running it);
every Claude response's usage is added to it, printed to the server log, and
reported on the results page and in review.json. Worker threads started by the
review carry the log with them (use submit()).

PRICES are US$ per million tokens and are an estimate only - check the bill in the
Claude Console. Cache writes cost 1.25x the input price (5 minute TTL) or 2x
(1 hour TTL); cache reads cost the read price.
"""

from __future__ import annotations

import contextvars
import threading
from concurrent.futures import Executor, Future
from dataclasses import asdict, dataclass, field

# model: (input, output, cache read) US$ per million tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}


@dataclass
class Call:
    step: str
    model: str
    input: int = 0  # uncached input tokens
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0  # includes thinking
    cost_usd: float | None = None


def _price(model: str) -> tuple[float, float, float] | None:
    for name, p in PRICES.items():
        if model == name or model.startswith(name + "-"):
            return p
    return None


def call_from(step: str, model: str, usage) -> Call:
    cc = getattr(usage, "cache_creation", None)
    w1h = int(getattr(cc, "ephemeral_1h_input_tokens", 0) or 0) if cc else 0
    w5m = int(getattr(cc, "ephemeral_5m_input_tokens", 0) or 0) if cc else 0
    total_w = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    if not cc:  # no breakdown: count it all as the TTL we ask for
        w1h, w5m = total_w, 0
    c = Call(step=step, model=model, input=int(getattr(usage, "input_tokens", 0) or 0),
             cache_write_5m=w5m, cache_write_1h=w1h,
             cache_read=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
             output=int(getattr(usage, "output_tokens", 0) or 0))
    p = _price(model)
    if p:
        inp, out, read = p
        c.cost_usd = round((c.input * inp + c.cache_write_5m * inp * 1.25 + c.cache_write_1h * inp * 2
                            + c.cache_read * read + c.output * out) / 1e6, 4)
    return c


@dataclass
class UsageLog:
    calls: list[Call] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, c: Call) -> None:
        with self.lock:
            self.calls.append(c)

    def summary(self) -> dict:
        with self.lock:
            calls = list(self.calls)
        keys = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
        totals = {k: sum(getattr(c, k) for c in calls) for k in keys}
        known = [c.cost_usd for c in calls if c.cost_usd is not None]
        all_input = totals["input"] + totals["cache_write_5m"] + totals["cache_write_1h"] + totals["cache_read"]
        return {
            "cache_checks": cache_checks(calls),
            "calls": [asdict(c) for c in calls],
            "totals": totals,
            "cache_hit_rate": round(totals["cache_read"] / all_input, 3) if all_input else 0.0,
            "cost_usd": round(sum(known), 2),
            "cost_complete": len(known) == len(calls),
        }


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def cache_checks(calls: list[Call]) -> list[dict]:
    """Did each step reuse what the step before it cached? One entry per hand-over:
    {"check", "ok": True/False/None (can't tell), "detail"}."""
    first: dict[str, Call] = {}
    for c in calls:
        first.setdefault(c.step, c)
    geo = first.get("Step 1 (geotech review)")
    design = first.get("Step 2 (architectural review)")
    sheets = [c for s, c in first.items() if s.startswith("Step 3")]
    out = []

    def verdict(name: str, c: Call, expected: int, why_not: str) -> None:
        w = c.cache_write_5m + c.cache_write_1h
        if expected < 1024:  # below the cacheable minimum: nothing to share
            out.append({"check": name, "ok": None, "detail": "too little shared content to cache"})
        elif c.cache_read >= 0.5 * expected:
            out.append({"check": name, "ok": True,
                        "detail": f"read {_k(c.cache_read)} tokens from cache, wrote {_k(w)}"})
        else:
            out.append({"check": name, "ok": False,
                        "detail": f"read only {_k(c.cache_read)} tokens from cache and wrote {_k(w)} - {why_not}"})

    def cached(c: Call) -> int:
        return c.cache_read + c.cache_write_5m + c.cache_write_1h

    # With ANTHROPIC_JSON_MODE=api the geotech and drawing reviews don't write a cache,
    # so these two checks only apply when they do.
    if geo and design and cached(geo):
        # Only the standards are shared between these two (the geotech report isn't).
        verdict("Geotech review -> drawing review (standards)", design, min(cached(geo), cached(design)),
                "the standards were sent again at full price")
    if design and sheets and cached(design):
        verdict("Drawing review -> first sheet (standards + drawings)", sheets[0], cached(design),
                "the first sheet did not reuse the drawing review's cache and cached it again "
                "(the other sheets reuse that); check ANTHROPIC_JSON_MODE is 'local'")
    if len(sheets) > 1:
        bad = [c for c in sheets[1:] if c.cache_read < 0.5 * cached(c)]
        if cached(sheets[0]) < 1024:
            out.append({"check": "First sheet -> other sheets", "ok": None, "detail": "too little to cache"})
        elif bad:
            out.append({"check": "First sheet -> other sheets", "ok": False,
                        "detail": f"{len(bad)} of {len(sheets) - 1} sheets sent the drawings again at full price: "
                                  + ", ".join(c.step for c in bad)})
        else:
            out.append({"check": "First sheet -> other sheets", "ok": True,
                        "detail": f"all {len(sheets) - 1} read the drawings and review results from cache"})
    return out


_current: contextvars.ContextVar[UsageLog | None] = contextvars.ContextVar("usage_log", default=None)


def start() -> UsageLog:
    log = UsageLog()
    _current.set(log)
    return log


def use(log: UsageLog) -> None:
    """Add this thread's calls to an existing review's log (e.g. a sheet re-run)."""
    _current.set(log)


def record(step: str, model: str, usage) -> None:
    if usage is None:
        return
    c = call_from(step, model, usage)
    log = _current.get()
    if log is not None:
        log.add(c)
    cost = f"${c.cost_usd:.3f}" if c.cost_usd is not None else "cost unknown"
    print(f"[usage] {step} ({model}): in {c.input}, cache write {c.cache_write_5m + c.cache_write_1h}, "
          f"cache read {c.cache_read}, out {c.output} -> {cost}", flush=True)


def submit(pool: Executor, fn, *args) -> Future:
    """pool.submit that carries this review's usage log (and other context) into the worker."""
    return pool.submit(contextvars.copy_context().run, fn, *args)
