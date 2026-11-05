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
            "calls": [asdict(c) for c in calls],
            "totals": totals,
            "cache_hit_rate": round(totals["cache_read"] / all_input, 3) if all_input else 0.0,
            "cost_usd": round(sum(known), 2),
            "cost_complete": len(known) == len(calls),
        }


_current: contextvars.ContextVar[UsageLog | None] = contextvars.ContextVar("usage_log", default=None)


def start() -> UsageLog:
    log = UsageLog()
    _current.set(log)
    return log


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
