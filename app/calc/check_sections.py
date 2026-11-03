"""Self-consistency check of the steel section catalogue.

Recomputes, from the plate dimensions alone:
  Ag   – plates + root fillets (UB/PFC) or rounded corners (SHS)      tolerance 5 %
  mass – Ag × 7850 kg/m³                                               tolerance 3 %
  Zx   – Ix / (d/2)   (sections symmetric about x)                    tolerance 2 %
  rx   – √(Ix/Ag), ry – √(Iy/Ag)                                       tolerance 1.5 %
  Ix   – rectangles; SHS with rounded corners, UB/PFC fillets ignored  tolerance 6 %  (advisory)
and prints anything inconsistent.  It cannot prove a value right – only catch typos.

Usage:  python -m app.calc.check_sections        (exit 1 only if a *verified* section fails)
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass

from .sections import Section, load_catalogue

RHO = 7850.0  # kg/m³

TOL = {"Ag": 0.05, "mass": 0.03, "Zx": 0.02, "rx": 0.015, "ry": 0.015, "Ix": 0.06}


@dataclass
class Issue:
    designation: str
    quantity: str
    catalogue: float
    recomputed: float
    error: float
    verified: bool

    def __str__(self) -> str:
        flag = "FAIL" if self.verified else "report"
        return f"[{flag}] {self.designation}: {self.quantity} catalogue={self.catalogue:.4g} recomputed={self.recomputed:.4g} ({self.error:+.1%})"


def area_from_plates(s: Section) -> float:
    if s.family == "SHS":
        B, D, t, ro = s.bf, s.d, s.tf, s.r1
        ri = max(ro - t, 0.0)
        return 2 * t * (B + D - 2 * t) - 4 * (1 - math.pi / 4) * (ro**2 - ri**2)
    fillets = (4 if s.family == "UB" else 2) * (1 - math.pi / 4) * s.r1**2
    return 2 * s.bf * s.tf + (s.d - 2 * s.tf) * s.tw + fillets


def _ix_rounded_rect(B: float, D: float, r: float) -> float:
    """Ix of a solid rectangle B×D with corners rounded to radius r.  Each corner loses the
    piece of the r×r square outside the quarter circle: area (1 − π/4)r², centroid 0.2234r
    from the outer edges (parallel-axis term only; the piece's own Ix is negligible)."""
    a = (1 - math.pi / 4) * r * r
    y = D / 2 - 0.2234 * r
    return B * D**3 / 12 - 4 * a * y * y


def ix_from_plates(s: Section) -> float:
    if s.family == "SHS":
        B, D, t, ro = s.bf, s.d, s.tf, s.r1
        ri = max(ro - t, 0.0)
        return _ix_rounded_rect(B, D, ro) - _ix_rounded_rect(B - 2 * t, D - 2 * t, ri)
    return (s.bf * s.d**3 - (s.bf - s.tw) * (s.d - 2 * s.tf) ** 3) / 12


def check_section(s: Section) -> list[Issue]:
    issues = []

    def cmp(q: str, cat: float, rec: float):
        err = (cat - rec) / rec
        if abs(err) > TOL[q]:
            issues.append(Issue(s.designation, q, cat, rec, err, s.verified))

    Ag = area_from_plates(s)
    cmp("Ag", s.Ag, Ag)
    cmp("mass", s.mass_kg_m, s.Ag * RHO * 1e-6)
    cmp("Zx", s.Zx, s.Ix / (s.d / 2))
    cmp("rx", s.rx, math.sqrt(s.Ix / s.Ag))
    cmp("ry", s.ry, math.sqrt(s.Iy / s.Ag))
    cmp("Ix", s.Ix, ix_from_plates(s))
    return issues


def run(verbose: bool = True) -> tuple[list[Issue], int, int]:
    """Returns (issues, n_sections, n_verified)."""
    issues: list[Issue] = []
    n = nv = 0
    for fam, secs in load_catalogue().items():
        for s in secs:
            n += 1
            nv += s.verified
            found = check_section(s)
            issues += found
            if verbose:
                tag = "verified" if s.verified else "UNVERIFIED (from memory)"
                print(f"{fam:3} {s.designation:16} {tag:26} {'ok' if not found else f'{len(found)} issue(s)'}")
    if verbose:
        for i in issues:
            print(" ", i)
        print(f"{n} sections, {nv} verified, {len(issues)} inconsistencies")
    return issues, n, nv


if __name__ == "__main__":
    iss, _, _ = run()
    sys.exit(1 if any(i.verified for i in iss) else 0)
