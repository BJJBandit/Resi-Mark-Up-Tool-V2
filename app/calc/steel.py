"""AS 4100 elastic member checks for the practice's small steel palette (UB bearers,
PFC lintels/roof beams, SHS posts).

Plain functions, each returning a small pydantic result with the governing clause and the
utilisation.  Internal units are N and mm; inputs/outputs in kN, kN/m, kNm, mm as named.
Clause numbers refer to AS 4100:1998 (incl. Amdt 1 2012; the 2020 edition keeps the same
numbering for everything used here).  Load combinations refer to AS/NZS 1170.0:2002.

Not covered (see README.md): web bearing (cl 5.13), composite action, fatigue, connections,
fire, wind uplift combinations.
"""
from __future__ import annotations

import math
import re
from typing import Literal, Sequence

from pydantic import BaseModel, Field

from .sections import Family, Section, load_catalogue

# --------------------------------------------------------------------------------------
# Constants – every value with its clause
# --------------------------------------------------------------------------------------
CONSTANTS = {
    "phi_bending": 0.9,         # AS 4100 Table 3.4 – member subject to bending (cl 5.1)
    "phi_shear": 0.9,           # AS 4100 Table 3.4 – shear (cl 5.11.1)
    "phi_compression": 0.9,     # AS 4100 Table 3.4 – axial compression (cl 6.1)
    "E": 200_000.0,             # MPa – AS 4100 cl 1.4 (Young's modulus of steel)
    "G": 80_000.0,              # MPa – AS 4100 cl 1.4 (shear modulus)
    "psi_s": 0.7,               # AS/NZS 1170.0 Table 4.1 – short-term factor, residential (domestic) floors
    "psi_l": 0.4,               # AS/NZS 1170.0 Table 4.1 – long-term factor, residential (domestic) floors
    "gamma_G": 1.2,             # AS/NZS 1170.0 cl 4.2.2(b) – 1.2G + 1.5Q
    "gamma_Q": 1.5,             # AS/NZS 1170.0 cl 4.2.2(b)
    "alpha_b_cold_formed_RHS": -0.5,   # AS 4100 Table 6.3.3(1) – cold-formed (non stress-relieved) RHS/CHS
    "alpha_b_hot_rolled_UB": 0.0,      # AS 4100 Table 6.3.3(1) – hot-rolled UB/UC, tf < 40 mm
    "alpha_b_hot_rolled_PFC": 0.5,     # AS 4100 Table 6.3.3(1) – "other sections" (channels, angles)
    "steel_density_kg_m3": 7850.0,     # AS 4100 cl 1.4 (used only for self-weight)
    "g_m_s2": 9.81,
}

# AS 4100 Table 5.2 plate element slenderness limits (λep, λey, λed) for bending, and
# Table 6.2.4 λey for axial compression, keyed by residual-stress category.
#   "outstand"      – one longitudinal edge supported, uniform compression (I flange)
#   "both_uniform"  – both edges supported, uniform compression (RHS flange)
#   "both_bending"  – both edges supported, compression at one edge / tension at other (web in bending)
TABLE_5_2 = {
    "outstand":     {"HR": (9, 16, 90),  "CF": (8, 15, 90),  "LW": (8, 15, 90),  "HW": (8, 14, 90),  "SR": (10, 16, 90)},
    "both_uniform": {"HR": (30, 45, 90), "CF": (30, 40, 90), "LW": (30, 35, 90), "HW": (30, 35, 90), "SR": (30, 45, 90)},
    "both_bending": {"HR": (82, 115, 180), "CF": (82, 115, 180), "LW": (82, 115, 180), "HW": (82, 115, 180), "SR": (82, 115, 180)},
}
TABLE_6_2_4 = {   # λey for axial compression (effective width, cl 6.2.4)
    "outstand":     {"HR": 16, "CF": 15, "LW": 15, "HW": 14, "SR": 16},
    "both":         {"HR": 45, "CF": 40, "LW": 35, "HW": 35, "SR": 45},
}

# AS 4100 Table 5.6.1 – a few moment-modification factors the user may choose to apply.
# Default alpha_m = 1.0 is conservative for every case.
TABLE_5_6_1_EXAMPLES = {
    "uniform_moment": 1.0,
    "udl_simply_supported": 1.13,
    "central_point_load_simply_supported": 1.35,
    "udl_one_end_fixed": 1.25,
}

# AS 4100 Tables 5.6.3(1)–(3) effective-length factors, defaults for a lintel/bearer
# segment restrained at both ends (FF) with the load applied at the top flange.
DEFAULT_KT = 1.0     # Table 5.6.3(1) – FF, FL, LL, FU restraint pairs
DEFAULT_KL = 1.4     # Table 5.6.3(2) – load within segment at the top (compression) flange; 1.0 at shear centre
DEFAULT_KR = 1.0     # Table 5.6.3(3) – no lateral rotation restraint at the segment ends (FU, PU, FF-not-rotation-restrained)

Support = Literal["simple", "continuous_2span"]
Restraint = Literal["full", "segment"]


# --------------------------------------------------------------------------------------
# Result models
# --------------------------------------------------------------------------------------
class CheckResult(BaseModel):
    name: str
    clause: str
    demand: float            # kNm, kN or mm
    capacity: float          # same unit as demand
    unit: str
    utilisation: float
    ok: bool
    notes: list[str] = Field(default_factory=list)


class BeamActions(BaseModel):
    """Design actions from :func:`beam_actions`. Moments kNm, shears kN."""
    support: Support
    span_mm: float
    w_G_kNm: float
    w_Q_kNm: float
    point_loads: list[tuple[float, float, float]] = Field(default_factory=list)
    M_star_kNm: float          # 1.2G + 1.5Q, maximum |M| along the beam
    V_star_kN: float           # 1.2G + 1.5Q, maximum |V|
    M_G_kNm: float             # characteristic components (for traceability)
    M_Q_kNm: float
    M_serv_short_kNm: float    # G + psi_s Q
    M_serv_long_kNm: float     # G + psi_l Q
    delta_G_x_EI: float        # mid-span deflection × EI under G alone (N·mm³); δ = value / (E·I)
    delta_Q_x_EI: float
    notes: list[str] = Field(default_factory=list)

    def deflections_mm(self, I_mm4: float, E: float = CONSTANTS["E"]) -> tuple[float, float]:
        """(δG, δQ) in mm for a section with second moment of area ``I_mm4``."""
        return self.delta_G_x_EI / (E * I_mm4), self.delta_Q_x_EI / (E * I_mm4)


class BeamCheck(BaseModel):
    designation: str
    family: str
    mass_kg_m: float
    verified: bool
    compactness: str
    checks: list[CheckResult]
    utilisation: float
    governing: str
    adequate: bool
    selected: bool = False
    trace: list[str] = Field(default_factory=list)


class ColumnCheck(BaseModel):
    designation: str
    family: str
    mass_kg_m: float
    verified: bool
    checks: list[CheckResult]
    utilisation: float
    governing: str
    adequate: bool
    selected: bool = False
    trace: list[str] = Field(default_factory=list)


class DeflectionLimit(BaseModel):
    ratio: int | None = None     # span/ratio
    max_mm: float | None = None
    text: str

    def limit_mm(self, span_mm: float) -> float:
        vals = []
        if self.ratio:
            vals.append(span_mm / self.ratio)
        if self.max_mm is not None:
            vals.append(self.max_mm)
        if not vals:
            raise ValueError(f"deflection limit {self.text!r} has neither span/N nor max mm")
        return min(vals)


def parse_deflection_limit(text: str) -> DeflectionLimit:
    """Parse preference strings such as ``"span/300"`` or ``"span/360, max 10 mm"``."""
    ratio = None
    max_mm = None
    m = re.search(r"span\s*/\s*(\d+)", text, re.I)
    if m:
        ratio = int(m.group(1))
    m = re.search(r"max\.?\s*([\d.]+)\s*mm", text, re.I)
    if m:
        max_mm = float(m.group(1))
    if ratio is None and max_mm is None:
        raise ValueError(f"cannot parse deflection limit {text!r}")
    return DeflectionLimit(ratio=ratio, max_mm=max_mm, text=text)


# --------------------------------------------------------------------------------------
# Loads – AS/NZS 1170.0 combinations and simple-beam statics
# --------------------------------------------------------------------------------------
def _simple_beam_envelope(L: float, w: float, points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Max |M| (N·mm) and max |V| (N) for a simply supported span L (mm) with UDL w (N/mm)
    and point loads [(x_mm, P_N)].  Evaluated on a fine grid plus the load positions; the
    bending moment of a UDL + point loads is piecewise quadratic so the grid is exact at the
    point-load positions and within <0.1 % elsewhere (grid 400 steps)."""
    RA = w * L / 2 + sum(P * (L - x) / L for x, P in points)
    xs = sorted({L * i / 400 for i in range(401)} | {x for x, _ in points})
    Mmax = 0.0
    for x in xs:
        M = RA * x - w * x * x / 2 - sum(P * (x - a) for a, P in points if a < x)
        Mmax = max(Mmax, abs(M))
    RB = w * L + sum(P for _, P in points) - RA
    V_ends = [abs(RA), abs(RB)]
    # shear jumps at the point loads – check just either side of each
    for a, _ in points:
        for xx in (a - 1e-6, a + 1e-6):
            V = RA - w * xx - sum(P for b, P in points if b < xx)
            V_ends.append(abs(V))
    return Mmax, max(V_ends)


def _simple_beam_midspan_deflection_x_EI(L: float, w: float, points: Sequence[tuple[float, float]]) -> float:
    """Mid-span deflection × EI (N·mm³) for simply supported span L:
    UDL        δ = 5 w L⁴ / (384 EI)
    point load δ = P a (3L² − 4a²) / (48 EI),  a = distance from the NEARER support (a ≤ L/2)
    (standard elastic formulas, e.g. Roark; superposition).  Mid-span deflection is used as
    the serviceability measure – for loads anywhere on the span the true maximum is within a
    few per cent of the mid-span value."""
    d = 5 * w * L**4 / 384
    for x, P in points:
        a = min(x, L - x)
        d += P * a * (3 * L * L - 4 * a * a) / 48
    return d


def beam_actions(span_mm: float, w_G_kNm: float, w_Q_kNm: float,
                 point_loads: Sequence[tuple[float, float, float]] = (),
                 support: Support = "simple") -> BeamActions:
    """Design actions for the AS/NZS 1170.0 combinations.

    ``point_loads`` = [(x_mm, P_G_kN, P_Q_kN), ...] measured from the left support.

    Strength:        1.2G + 1.5Q                       (AS/NZS 1170.0 cl 4.2.2(b))
    Serviceability:  G + ψs Q (short-term), G + ψl Q (long-term)   (cl 4.3, Table 4.1:
                     ψs = 0.7, ψl = 0.4 for residential/domestic floors)

    ``support="simple"``: simply supported, UDL + point loads, envelope by statics.
    ``support="continuous_2span"``: two equal spans of ``span_mm`` each, UDL on both spans,
    prismatic, no point loads (raises if any are given):
        M_support = w L² / 8 (hogging, governs),  M_span = 9 w L² / 128,
        V_max = 5 w L / 8 (at the central support),
        δ_max = w L⁴ / (185 EI)   (= 0.00541 w L⁴/EI, in each span).
    """
    L = float(span_mm)
    notes = []
    gG, gQ, ps, pl = CONSTANTS["gamma_G"], CONSTANTS["gamma_Q"], CONSTANTS["psi_s"], CONSTANTS["psi_l"]
    wG, wQ = w_G_kNm, w_Q_kNm                      # kN/m == N/mm numerically
    pts_G = [(x, PG * 1e3) for x, PG, _ in point_loads]
    pts_Q = [(x, PQ * 1e3) for x, _, PQ in point_loads]

    if support == "simple":
        M_G, V_G = _simple_beam_envelope(L, wG, pts_G)
        M_Q, V_Q = _simple_beam_envelope(L, wQ, pts_Q)
        M_u, V_u = _simple_beam_envelope(L, gG * wG + gQ * wQ, [(x, gG * PG * 1e3 + gQ * PQ * 1e3) for x, PG, PQ in point_loads])
        dG = _simple_beam_midspan_deflection_x_EI(L, wG, pts_G)
        dQ = _simple_beam_midspan_deflection_x_EI(L, wQ, pts_Q)
        notes.append("simply supported; M*/V* by statics envelope; δ at mid-span by superposition (5wL⁴/384EI, Pa(3L²−4a²)/48EI)")
    elif support == "continuous_2span":
        if point_loads:
            raise ValueError("continuous_2span supports UDL only (point loads not implemented)")
        M_G, V_G = wG * L * L / 8, 5 * wG * L / 8
        M_Q, V_Q = wQ * L * L / 8, 5 * wQ * L / 8
        wu = gG * wG + gQ * wQ
        M_u, V_u = wu * L * L / 8, 5 * wu * L / 8
        dG, dQ = wG * L**4 / 185, wQ * L**4 / 185
        notes.append("two equal spans, UDL both spans: M = wL²/8 at central support, V = 5wL/8, δ = wL⁴/185EI")
    else:
        raise ValueError(f"unknown support {support!r}")

    for x, PG, PQ in point_loads:
        if not 0 <= x <= L:
            raise ValueError(f"point load at x={x} outside span 0..{L}")

    return BeamActions(
        support=support, span_mm=L, w_G_kNm=wG, w_Q_kNm=wQ, point_loads=list(point_loads),
        M_star_kNm=M_u / 1e6, V_star_kN=V_u / 1e3,
        M_G_kNm=M_G / 1e6, M_Q_kNm=M_Q / 1e6,
        M_serv_short_kNm=(M_G + ps * M_Q) / 1e6, M_serv_long_kNm=(M_G + pl * M_Q) / 1e6,
        delta_G_x_EI=dG, delta_Q_x_EI=dQ,
        notes=notes + [f"1.2G+1.5Q (AS/NZS 1170.0 cl 4.2.2); ψs={ps}, ψl={pl} (Table 4.1 residential)"],
    )


# --------------------------------------------------------------------------------------
# Section classification – AS 4100 cl 5.2 (bending) and cl 6.2 (compression)
# --------------------------------------------------------------------------------------
class PlateClass(BaseModel):
    element: str
    b: float
    t: float
    lam_e: float
    lam_ep: float
    lam_ey: float
    klass: Literal["compact", "non-compact", "slender"]


def plate_slenderness(b: float, t: float, fy: float) -> float:
    """λe = (b/t)·√(fy/250)   – AS 4100 cl 5.2.2 (eq 5.2.2)."""
    return (b / t) * math.sqrt(fy / 250.0)


def classify_plate(b: float, t: float, fy: float, element: Literal["outstand", "both_uniform", "both_bending"],
                   residual: str = "HR", name: str = "") -> PlateClass:
    """Classify one flat plate element per AS 4100 Table 5.2."""
    lam_ep, lam_ey, _ = TABLE_5_2[element][residual]
    lam_e = plate_slenderness(b, t, fy)
    klass = "compact" if lam_e <= lam_ep else "non-compact" if lam_e <= lam_ey else "slender"
    return PlateClass(element=name or element, b=b, t=t, lam_e=lam_e, lam_ep=lam_ep, lam_ey=lam_ey, klass=klass)


def section_plates_bending(s: Section) -> list[PlateClass]:
    """Flat plate elements of a section bent about x (AS 4100 cl 5.2.2, Table 5.2).

    Clear widths (cl 5.2.2 'clear width of the element'):
      UB  : flange outstand b = (bf − tw)/2 (one edge supported); web b = d − 2tf (both edges)
      PFC : flange outstand b = bf − tw;      web b = d − 2tf
      SHS : flange b = B − 2t (both edges, uniform compression); web b = D − 2t
    Note: the root radius r1 is NOT deducted from the clear width – deducting it (as the
    brief suggested) is slightly unconservative relative to cl 5.2.2 and the ASI tables.
    """
    res = s.residual_stress
    if s.family == "SHS":
        return [classify_plate(s.bf - 2 * s.tf, s.tf, s.fy_flange, "both_uniform", res, "flange (B−2t)"),
                classify_plate(s.d - 2 * s.tf, s.tw, s.fy_web, "both_bending", res, "web (D−2t)")]
    if s.family == "PFC":
        fl_b = s.bf - s.tw
    else:
        fl_b = (s.bf - s.tw) / 2
    return [classify_plate(fl_b, s.tf, s.fy_flange, "outstand", res, "flange outstand"),
            classify_plate(s.d - 2 * s.tf, s.tw, s.fy_web, "both_bending", res, "web")]


class SectionMoment(BaseModel):
    Ze: float                 # mm³
    Ms_kNm: float
    phiMs_kNm: float
    compactness: Literal["compact", "non-compact", "slender"]
    lam_s: float
    lam_sp: float
    lam_sy: float
    plates: list[PlateClass]
    clause: str
    notes: list[str] = Field(default_factory=list)


def section_moment_capacity(s: Section) -> SectionMoment:
    """φMs = φ fy Ze about the major axis – AS 4100 cl 5.2.

    Section slenderness λs, λsp, λsy are those of the element with the greatest λe/λey
    (cl 5.2.2).  Zc = min(S, 1.5Z) (cl 5.2.3).
      compact     (λs ≤ λsp): Ze = Zc                                        cl 5.2.3
      non-compact (λsp < λs ≤ λsy): Ze = Z + [(λsy − λs)/(λsy − λsp)](Zc − Z)   cl 5.2.4
      slender     (λs > λsy): Ze = Z (λsy/λs)   [flat plates in uniform compression]   cl 5.2.5(a)
                  (the (λsy/λs)² form of cl 5.2.5(b) applies to outstands with max compression
                  at the unsupported edge and tension at the other – not our flange case)
    """
    plates = section_plates_bending(s)
    gov = max(plates, key=lambda p: p.lam_e / p.lam_ey)
    lam_s, lam_sp, lam_sy = gov.lam_e, gov.lam_ep, gov.lam_ey
    fy = s.fy
    Z, S = s.Zx, s.Sx
    Zc = min(S, 1.5 * Z)
    notes = [f"governing element {gov.element}: λe={lam_s:.1f} (λep={lam_sp}, λey={lam_sy}) → {gov.klass}"]
    if lam_s <= lam_sp:
        Ze, klass, cl = Zc, "compact", "5.2.3"
    elif lam_s <= lam_sy:
        Ze = Z + ((lam_sy - lam_s) / (lam_sy - lam_sp)) * (Zc - Z)
        klass, cl = "non-compact", "5.2.4"
    else:
        Ze, klass, cl = Z * (lam_sy / lam_s), "slender", "5.2.5(a)"
    Ms = fy * Ze
    phi = CONSTANTS["phi_bending"]
    notes.append(f"Ze={Ze:.3e} mm³, fy={fy:.0f} MPa, φ={phi}")
    return SectionMoment(Ze=Ze, Ms_kNm=Ms / 1e6, phiMs_kNm=phi * Ms / 1e6, compactness=klass,
                         lam_s=lam_s, lam_sp=lam_sp, lam_sy=lam_sy, plates=plates, clause=f"AS 4100 cl 5.2 ({cl})", notes=notes)


# --------------------------------------------------------------------------------------
# Member moment capacity – AS 4100 cl 5.6
# --------------------------------------------------------------------------------------
class MemberMoment(BaseModel):
    Le_mm: float
    Mo_kNm: float | None
    alpha_s: float
    alpha_m: float
    Mb_kNm: float
    phiMb_kNm: float
    clause: str
    notes: list[str] = Field(default_factory=list)


def reference_buckling_moment(s: Section, Le_mm: float) -> float:
    """Mo = √[(π²E Iy / Le²)(G J + π²E Iw / Le²)]  – AS 4100 eq 5.6.1.1(3).  Returns N·mm."""
    E, G = CONSTANTS["E"], CONSTANTS["G"]
    a = math.pi**2 * E * s.Iy / Le_mm**2
    return math.sqrt(a * (G * s.J + math.pi**2 * E * s.Iw / Le_mm**2))


def member_moment_capacity(s: Section, Ms_kNm: float, span_mm: float, restraint: Restraint = "full",
                           alpha_m: float = 1.0, Le_mm: float | None = None,
                           kt: float = DEFAULT_KT, kl: float = DEFAULT_KL, kr: float = DEFAULT_KR) -> MemberMoment:
    """φMb = φ αm αs Ms ≤ φMs – AS 4100 cl 5.6.1.1.

    restraint="full"    : segment fully laterally restrained (cl 5.3), αs = 1 → Mb = Ms
                          (lintels/bearers with floor or roof framing fixed to the top flange).
    restraint="segment" : Le = kt·kl·kr·L (cl 5.6.3, Tables 5.6.3(1)–(3); defaults 1.0, 1.4, 1.0),
                          αs = 0.6[√((Ms/Mo)² + 3) − Ms/Mo]  (eq 5.6.1.1(2)), Mo from eq 5.6.1.1(3).
    αm: user value, default 1.0 (conservative). See TABLE_5_6_1_EXAMPLES / Table 5.6.1.
    SHS: Iw = 0, J large → αs ≈ 1; AS 4100 cl 5.1 permits Mb = Ms for RHS/SHS bent about
    the major axis when b ≈ d (treated the same way here through the formula).
    """
    phi = CONSTANTS["phi_bending"]
    Ms = Ms_kNm * 1e6
    notes = []
    if restraint == "full":
        Le = span_mm
        Mo = None
        alpha_s = 1.0
        notes.append("full lateral restraint (cl 5.3.2.4 / 5.6.1.1 with Le→0): αs = 1.0")
    elif restraint == "segment":
        Le = Le_mm if Le_mm is not None else kt * kl * kr * span_mm
        Mo = reference_buckling_moment(s, Le)
        r = Ms / Mo
        alpha_s = 0.6 * (math.sqrt(r * r + 3) - r)
        notes.append(f"Le = {Le:.0f} mm" + ("" if Le_mm is not None else f" = kt·kl·kr·L = {kt}·{kl}·{kr}·{span_mm:.0f} (Tables 5.6.3(1)–(3))"))
        notes.append(f"Mo = {Mo/1e6:.1f} kNm (eq 5.6.1.1(3)), αs = {alpha_s:.3f} (eq 5.6.1.1(2))")
    else:
        raise ValueError(f"unknown restraint {restraint!r}")
    Mb = min(alpha_m * alpha_s * Ms, Ms)
    notes.append(f"αm = {alpha_m} (Table 5.6.1; 1.0 conservative)")
    return MemberMoment(Le_mm=Le, Mo_kNm=None if Mo is None else Mo / 1e6, alpha_s=alpha_s, alpha_m=alpha_m,
                        Mb_kNm=Mb / 1e6, phiMb_kNm=phi * Mb / 1e6, clause="AS 4100 cl 5.6.1.1", notes=notes)


# --------------------------------------------------------------------------------------
# Shear – AS 4100 cl 5.11 (+ cl 5.12 interaction)
# --------------------------------------------------------------------------------------
class ShearCapacity(BaseModel):
    Aw: float
    dp_tw: float
    dp_tw_limit: float
    alpha_v: float
    Vv_kN: float
    phiVv_kN: float
    clause: str
    notes: list[str] = Field(default_factory=list)


def shear_capacity(s: Section) -> ShearCapacity:
    """Web shear capacity, unstiffened web, approximately uniform shear stress (cl 5.11.2).

    Vw = 0.6 fy Aw  (cl 5.11.4) with Aw = d·tw for hot-rolled I/channel sections; for SHS the
    two webs give Aw = 2·(d − 2t)·t... AS 4100 cl 5.11.4 takes Aw as the gross web area; for
    RHS the ASI DCTs use Aw = 2 d t (both webs, overall depth) – adopted here.
    If dp/tw ≤ 82/√(fy/250) the web yields (Vv = Vw, cl 5.11.2(a)); otherwise buckling governs,
    Vv = αv Vw with αv = [82/((dp/tw)√(fy/250))]² (cl 5.11.5.1, unstiffened web).
    dp = clear depth of web = d − 2tf.
    """
    fy = s.fy_web
    if s.family == "SHS":
        Aw = 2 * s.d * s.tw
        dp = s.d - 2 * s.tf
    else:
        Aw = s.d * s.tw
        dp = s.d - 2 * s.tf
    Vw = 0.6 * fy * Aw
    dp_tw = dp / s.tw
    limit = 82 / math.sqrt(fy / 250)
    if dp_tw <= limit:
        alpha_v, cl = 1.0, "5.11.2(a)/5.11.4"
    else:
        alpha_v, cl = (82 / (dp_tw * math.sqrt(fy / 250))) ** 2, "5.11.5.1"
    Vv = alpha_v * Vw
    phi = CONSTANTS["phi_shear"]
    return ShearCapacity(Aw=Aw, dp_tw=dp_tw, dp_tw_limit=limit, alpha_v=alpha_v, Vv_kN=Vv / 1e3, phiVv_kN=phi * Vv / 1e3,
                         clause=f"AS 4100 cl {cl}", notes=[f"Aw={Aw:.0f} mm², dp/tw={dp_tw:.1f} (limit {limit:.1f}), fy_web={fy:.0f}"])


def shear_moment_interaction(phiVv_kN: float, M_star_kNm: float, phiMs_kNm: float) -> tuple[float, str]:
    """cl 5.12.3: Vvm = Vv for M* ≤ 0.75 φMs, else Vv[2.2 − 1.6 M*/(φMs)]  (web shear with bending)."""
    if M_star_kNm <= 0.75 * phiMs_kNm:
        return phiVv_kN, "cl 5.12.2 (M* ≤ 0.75φMs, no reduction)"
    f = max(2.2 - 1.6 * M_star_kNm / phiMs_kNm, 0.0)
    return phiVv_kN * f, f"cl 5.12.3 (Vvm = Vv·{f:.3f})"


# --------------------------------------------------------------------------------------
# Beam check and selection
# --------------------------------------------------------------------------------------
def _trace_header(s: Section) -> list[str]:
    t = [f"section {s.designation} – {s.standard} {s.grade}; source: {s.source}"]
    if not s.verified:
        t.append("section properties unverified – catalogue row marked verified: false")
    return t


def check_beam(s: Section, actions: BeamActions, deflection_limit: str | DeflectionLimit = "span/300",
               live_deflection_limit: str | DeflectionLimit | None = None, restraint: Restraint = "full",
               alpha_m: float = 1.0, Le_mm: float | None = None, include_self_weight: bool = True) -> BeamCheck:
    """All beam checks for one section: bending (cl 5.1/5.2/5.6), shear (cl 5.11/5.12),
    deflection (AS/NZS 1170.0 App. C limits from preferences).  Web bearing (cl 5.13) TODO."""
    trace = _trace_header(s)
    if include_self_weight and s.mass_kg_m > 0:
        sw = s.mass_kg_m * CONSTANTS["g_m_s2"] / 1e3   # kN/m
        actions = beam_actions(actions.span_mm, actions.w_G_kNm + sw, actions.w_Q_kNm, actions.point_loads, actions.support)
        trace.append(f"self-weight {sw:.3f} kN/m added to G")
    L = actions.span_mm
    checks: list[CheckResult] = []

    sm = section_moment_capacity(s)
    mm = member_moment_capacity(s, sm.Ms_kNm, L, restraint, alpha_m, Le_mm)
    phiM = min(sm.phiMs_kNm, mm.phiMb_kNm)
    checks.append(CheckResult(name="bending", clause=f"{sm.clause}; {mm.clause}", demand=actions.M_star_kNm,
                              capacity=phiM, unit="kNm", utilisation=actions.M_star_kNm / phiM, ok=actions.M_star_kNm <= phiM,
                              notes=sm.notes + mm.notes + [f"φMs={sm.phiMs_kNm:.1f} kNm, φMb={mm.phiMb_kNm:.1f} kNm"]))

    sh = shear_capacity(s)
    phiV, cl12 = shear_moment_interaction(sh.phiVv_kN, actions.M_star_kNm, sm.phiMs_kNm)
    checks.append(CheckResult(name="shear", clause=f"{sh.clause}; {cl12}", demand=actions.V_star_kN, capacity=phiV, unit="kN",
                              utilisation=actions.V_star_kN / phiV, ok=actions.V_star_kN <= phiV, notes=sh.notes))

    dl = parse_deflection_limit(deflection_limit) if isinstance(deflection_limit, str) else deflection_limit
    dG, dQ = actions.deflections_mm(s.Ix)
    psi_s = CONSTANTS["psi_s"]
    d_total = dG + psi_s * dQ
    lim = dl.limit_mm(L)
    checks.append(CheckResult(name="deflection_total", clause="AS/NZS 1170.0 cl 4.3 (G + ψs Q), App. C; limit from preferences",
                              demand=d_total, capacity=lim, unit="mm", utilisation=d_total / lim, ok=d_total <= lim,
                              notes=[f"δG={dG:.2f} mm, δQ={dQ:.2f} mm, ψs={psi_s}; limit {dl.text} → {lim:.1f} mm; E={CONSTANTS['E']:.0f}, Ix={s.Ix:.3e}"]))
    if live_deflection_limit:
        dll = parse_deflection_limit(live_deflection_limit) if isinstance(live_deflection_limit, str) else live_deflection_limit
        lim_l = dll.limit_mm(L)
        d_live = psi_s * dQ
        checks.append(CheckResult(name="deflection_live", clause="AS/NZS 1170.0 cl 4.3 (ψs Q), App. C; limit from preferences",
                                  demand=d_live, capacity=lim_l, unit="mm", utilisation=d_live / lim_l, ok=d_live <= lim_l,
                                  notes=[f"ψs·δQ, limit {dll.text} → {lim_l:.1f} mm"]))

    checks.append(CheckResult(name="web_bearing", clause="AS 4100 cl 5.13 – TODO (not checked)", demand=0.0, capacity=0.0, unit="kN",
                              utilisation=0.0, ok=True, notes=["bearing yield/buckling at supports and under point loads not yet implemented"]))

    gov = max((c for c in checks if c.capacity > 0), key=lambda c: c.utilisation)
    return BeamCheck(designation=s.designation, family=s.family, mass_kg_m=s.mass_kg_m, verified=s.verified,
                     compactness=sm.compactness, checks=checks, utilisation=gov.utilisation, governing=gov.name,
                     adequate=all(c.ok for c in checks), trace=trace + actions.notes)


def design_beam(section_family: Family, span_mm: float, w_G_kNm: float, w_Q_kNm: float,
                point_loads: Sequence[tuple[float, float, float]] = (), support: Support = "simple",
                deflection_limit: str = "span/300", live_deflection_limit: str | None = None,
                restraint: Restraint = "full", alpha_m: float = 1.0, Le_mm: float | None = None,
                include_self_weight: bool = True, catalogue_dir: str | None = None) -> list[BeamCheck]:
    """Walk the family from lightest to heaviest; every section's utilisation is returned and
    the first adequate one is marked ``selected``."""
    actions = beam_actions(span_mm, w_G_kNm, w_Q_kNm, point_loads, support)
    out = []
    picked = False
    for s in load_catalogue(catalogue_dir)[section_family]:
        bc = check_beam(s, actions, deflection_limit, live_deflection_limit, restraint, alpha_m, Le_mm, include_self_weight)
        if bc.adequate and not picked:
            bc.selected = True
            picked = True
        out.append(bc)
    return out


# --------------------------------------------------------------------------------------
# Columns – AS 4100 Section 6 (and 8 for combined actions)
# --------------------------------------------------------------------------------------
class SectionCompression(BaseModel):
    kf: float
    An: float
    Ns_kN: float
    phiNs_kN: float
    clause: str
    notes: list[str] = Field(default_factory=list)


def form_factor(s: Section) -> tuple[float, list[str]]:
    """kf = Ae/Ag – cl 6.2.2, with effective widths be = b·min(λey/λe, 1) – cl 6.2.4, Table 6.2.4.
    Elements: SHS – four walls of clear width B−2t, both edges supported;
              UB – two flanges (4 outstands (bf−tw)/2) and a web d−2tf;
              PFC – two outstands bf−tw and a web d−2tf.  Corners/roots counted fully effective."""
    res = s.residual_stress
    notes = []
    if s.family == "SHS":
        b = s.bf - 2 * s.tf
        lam = plate_slenderness(b, s.tf, s.fy)
        ley = TABLE_6_2_4["both"][res]
        be = b * min(ley / lam, 1.0)
        ineffective = 4 * (b - be) * s.tf
        notes.append(f"wall b={b:.1f}, λe={lam:.1f}, λey={ley} → be={be:.1f}")
    else:
        n_out = 4 if s.family == "UB" else 2
        bo = (s.bf - s.tw) / 2 if s.family == "UB" else s.bf - s.tw
        lam_f = plate_slenderness(bo, s.tf, s.fy_flange)
        ley_f = TABLE_6_2_4["outstand"][res]
        be_f = bo * min(ley_f / lam_f, 1.0)
        bw = s.d - 2 * s.tf
        lam_w = plate_slenderness(bw, s.tw, s.fy_web)
        ley_w = TABLE_6_2_4["both"][res]
        be_w = bw * min(ley_w / lam_w, 1.0)
        ineffective = n_out * (bo - be_f) * s.tf + (bw - be_w) * s.tw
        notes.append(f"flange outstand λe={lam_f:.1f} (λey {ley_f}); web λe={lam_w:.1f} (λey {ley_w})")
    kf = (s.Ag - ineffective) / s.Ag
    return kf, notes


def section_compression_capacity(s: Section) -> SectionCompression:
    """φNs = φ kf An fy – AS 4100 cl 6.2.1 (An = Ag, no holes)."""
    kf, notes = form_factor(s)
    Ns = kf * s.Ag * s.fy
    phi = CONSTANTS["phi_compression"]
    return SectionCompression(kf=kf, An=s.Ag, Ns_kN=Ns / 1e3, phiNs_kN=phi * Ns / 1e3, clause="AS 4100 cl 6.2.1 (kf cl 6.2.2/6.2.4)",
                              notes=notes + [f"kf={kf:.3f}, An={s.Ag:.0f} mm², fy={s.fy:.0f}"])


class MemberCompression(BaseModel):
    Le_mm: float
    lam_n: float
    alpha_a: float
    alpha_b: float
    lam: float
    alpha_c: float
    Nc_kN: float
    phiNc_kN: float
    clause: str
    notes: list[str] = Field(default_factory=list)


def alpha_b_for(s: Section) -> float:
    """Member section constant αb – AS 4100 Table 6.3.3(1)."""
    if s.family == "SHS":
        return CONSTANTS["alpha_b_cold_formed_RHS"] if s.residual_stress == "CF" else -1.0
    if s.family == "UB":
        return CONSTANTS["alpha_b_hot_rolled_UB"]
    return CONSTANTS["alpha_b_hot_rolled_PFC"]


def slenderness_reduction_factor(lam_n: float, alpha_b: float) -> tuple[float, float, float]:
    """αc per AS 4100 cl 6.3.3 (closed form of Table 6.3.3(3)).  Returns (αa, λ, αc)."""
    alpha_a = 2100 * (lam_n - 13.5) / (lam_n**2 - 15.3 * lam_n + 2050)
    lam = lam_n + alpha_a * alpha_b
    eta = max(0.00326 * (lam - 13.5), 0.0)
    if lam <= 0:
        return alpha_a, lam, 1.0
    xi = ((lam / 90) ** 2 + 1 + eta) / (2 * (lam / 90) ** 2)
    inner = 1 - (90 / (xi * lam)) ** 2
    alpha_c = xi * (1 - math.sqrt(max(inner, 0.0)))
    return alpha_a, lam, min(alpha_c, 1.0)


def member_compression_capacity(s: Section, L_mm: float, ke: float = 1.0, axis: Literal["x", "y", "min"] = "min",
                                kf: float | None = None) -> MemberCompression:
    """φNc = φ αc Ns ≤ φNs – AS 4100 cl 6.3.3.
    Le = ke·L (cl 6.3.2, ke from Fig 4.6.3.2: 1.0 pinned-pinned, 0.7 fixed-fixed, 2.2 fixed-free ...).
    λn = (Le/r)·√kf·√(fy/250) (eq 6.3.3(1)); αa, λ, η, ξ, αc per cl 6.3.3; αb per Table 6.3.3(1)."""
    sc = section_compression_capacity(s)
    kf = sc.kf if kf is None else kf
    r = {"x": s.rx, "y": s.ry, "min": min(s.rx, s.ry)}[axis]
    Le = ke * L_mm
    lam_n = (Le / r) * math.sqrt(kf) * math.sqrt(s.fy / 250)
    ab = alpha_b_for(s)
    alpha_a, lam, alpha_c = slenderness_reduction_factor(lam_n, ab)
    Nc = min(alpha_c * sc.Ns_kN, sc.Ns_kN)
    phi = CONSTANTS["phi_compression"]
    return MemberCompression(Le_mm=Le, lam_n=lam_n, alpha_a=alpha_a, alpha_b=ab, lam=lam, alpha_c=alpha_c, Nc_kN=Nc, phiNc_kN=phi * Nc,
                             clause="AS 4100 cl 6.3.3 (Le cl 6.3.2 / Fig 4.6.3.2; αb Table 6.3.3(1))",
                             notes=[f"Le={Le:.0f} mm (ke={ke}), r={r:.1f} mm, λn={lam_n:.1f}, αa={alpha_a:.2f}, αb={ab}, λ={lam:.1f}, αc={alpha_c:.3f}"])


def check_column(s: Section, N_star_kN: float, L_mm: float, ke: float = 1.0, M_star_kNm: float = 0.0,
                 eccentricity_mm: float = 0.0, My_star_kNm: float = 0.0, restraint: Restraint = "full") -> ColumnCheck:
    """Axial compression (cl 6) plus the simplified combined-actions checks of cl 8.3 / 8.4
    for a column with a small moment (applied M* and/or N*·e).

    Section (cl 8.3.2 reduced moment):  Mrx = Msx (1 − N*/φNs)              ≥ M*
    Biaxial section (cl 8.3.4):          (Mx*/φMrx)^1.4 + (My*/φMry)^1.4 ≤ 1  (when My* given)
    In-plane member (cl 8.4.2.2):        Mix = Msx (1 − N*/φNcx)             ≥ M*
    Out-of-plane member (cl 8.4.4.1):    Mox = Mbx (1 − N*/φNcy)             ≥ M*
    For SHS Mbx = Msx (restraint "full" or Mo very large), so the out-of-plane form reduces
    to the in-plane one with the minor-axis Nc.  Second-order amplification (δb, cl 4.4.2) is
    NOT applied – eccentricity moments on short posts are small; flag if N*/φNc > 0.9."""
    trace = _trace_header(s)
    checks: list[CheckResult] = []
    phi = CONSTANTS["phi_compression"]
    sc = section_compression_capacity(s)
    mcx = member_compression_capacity(s, L_mm, ke, "x", sc.kf)
    mcy = member_compression_capacity(s, L_mm, ke, "y", sc.kf)
    phiNc = min(mcx.phiNc_kN, mcy.phiNc_kN)
    checks.append(CheckResult(name="axial_section", clause=sc.clause, demand=N_star_kN, capacity=sc.phiNs_kN, unit="kN",
                              utilisation=N_star_kN / sc.phiNs_kN, ok=N_star_kN <= sc.phiNs_kN, notes=sc.notes))
    checks.append(CheckResult(name="axial_member", clause=mcy.clause, demand=N_star_kN, capacity=phiNc, unit="kN",
                              utilisation=N_star_kN / phiNc, ok=N_star_kN <= phiNc, notes=mcx.notes + mcy.notes))

    Mx = M_star_kNm + N_star_kN * eccentricity_mm / 1e3
    if Mx > 0 or My_star_kNm > 0:
        sm = section_moment_capacity(s)
        mm = member_moment_capacity(s, sm.Ms_kNm, L_mm, restraint)
        phiMsx = sm.phiMs_kNm
        phiMbx = min(mm.phiMb_kNm, phiMsx)
        # section: cl 8.3.2
        phiMrx = max(phiMsx * (1 - N_star_kN / sc.phiNs_kN), 0.0)
        if My_star_kNm > 0:
            # minor axis: for SHS Zy = Zx; for UB/PFC use elastic Zy (conservative) – cl 8.3.3 Mry
            Zey = min(s.Sy, 1.5 * s.Zy)
            phiMsy = CONSTANTS["phi_bending"] * s.fy * Zey / 1e6
            phiMry = max(phiMsy * (1 - N_star_kN / sc.phiNs_kN), 0.0)
            u = (Mx / phiMrx) ** 1.4 + (My_star_kNm / phiMry) ** 1.4 if phiMrx > 0 and phiMry > 0 else math.inf
            checks.append(CheckResult(name="combined_section_biaxial", clause="AS 4100 cl 8.3.4 (with 8.3.2/8.3.3 reduced moments)",
                                      demand=u, capacity=1.0, unit="-", utilisation=u, ok=u <= 1.0,
                                      notes=[f"Mx*={Mx:.2f}, φMrx={phiMrx:.2f}; My*={My_star_kNm:.2f}, φMry={phiMry:.2f} kNm"]))
        else:
            # M* ≤ φMsx(1 − N*/φNs)  ⇔  N*/φNs + M*/φMsx ≤ 1 – reported as that sum so the
            # utilisation reads as a total and is never below the pure axial ratio
            u = N_star_kN / sc.phiNs_kN + Mx / phiMsx
            checks.append(CheckResult(name="combined_section", clause="AS 4100 cl 8.3.2 (Mrx = Msx(1 − N*/φNs))", demand=u, capacity=1.0,
                                      unit="-", utilisation=u, ok=u <= 1.0,
                                      notes=[f"Mx*={Mx:.2f} kNm, φMsx={phiMsx:.2f} kNm, φMrx={phiMrx:.2f} kNm, N*/φNs={N_star_kN / sc.phiNs_kN:.3f}"]))
        # member: cl 8.4.2.2 (in-plane) and 8.4.4.1 (out-of-plane)
        phiMix = max(phiMsx * (1 - N_star_kN / mcx.phiNc_kN), 0.0)
        phiMox = max(phiMbx * (1 - N_star_kN / mcy.phiNc_kN), 0.0)
        phiMm = min(phiMix, phiMox)
        notes = [f"φMix={phiMix:.2f} (cl 8.4.2.2, φNcx={mcx.phiNc_kN:.1f}), φMox={phiMox:.2f} (cl 8.4.4.1, φNcy={mcy.phiNc_kN:.1f}, φMbx={phiMbx:.2f})",
                 "second-order amplification δb (cl 4.4.2.2) not applied"]
        if N_star_kN / phiNc > 0.9:
            notes.append("N*/φNc > 0.9 – moment amplification would matter; refine")
        # in-plane  M* ≤ φMsx(1 − N*/φNcx) ⇔ N*/φNcx + M*/φMsx ≤ 1 ; out-of-plane with φNcy, φMbx
        u = max(N_star_kN / mcx.phiNc_kN + Mx / phiMsx, N_star_kN / mcy.phiNc_kN + Mx / phiMbx)
        notes.append(f"Mx*={Mx:.2f} kNm; utilisation = max(N*/φNcx + Mx*/φMsx, N*/φNcy + Mx*/φMbx)")
        checks.append(CheckResult(name="combined_member", clause="AS 4100 cl 8.4.2.2 / 8.4.4.1 (simplified)", demand=u, capacity=1.0, unit="-",
                                  utilisation=u, ok=u <= 1.0 and Mx <= phiMm, notes=notes))
        trace.append(f"eccentricity moment N*·e = {N_star_kN * eccentricity_mm / 1e3:.2f} kNm added to Mx*")

    gov = max(checks, key=lambda c: c.utilisation)
    return ColumnCheck(designation=s.designation, family=s.family, mass_kg_m=s.mass_kg_m, verified=s.verified, checks=checks,
                       utilisation=gov.utilisation, governing=gov.name, adequate=all(c.ok for c in checks), trace=trace)


def design_column(section_family: Family, N_star_kN: float, L_mm: float, ke: float = 1.0, M_star_kNm: float = 0.0,
                  eccentricity_mm: float = 0.0, My_star_kNm: float = 0.0, catalogue_dir: str | None = None) -> list[ColumnCheck]:
    """Walk the family lightest → heaviest; first adequate section marked ``selected``."""
    out = []
    picked = False
    for s in load_catalogue(catalogue_dir)[section_family]:
        cc = check_column(s, N_star_kN, L_mm, ke, M_star_kNm, eccentricity_mm, My_star_kNm)
        if cc.adequate and not picked:
            cc.selected = True
            picked = True
        out.append(cc)
    return out
