"""Tests for the AS 4100 steel member engine (app/calc/steel.py) and section catalogue."""
import math

import pytest

from app.calc import check_sections
from app.calc.sections import get_section, load_catalogue
from app.calc.steel import (
    CONSTANTS, beam_actions, check_column, classify_plate, design_beam, design_column,
    member_compression_capacity, parse_deflection_limit, section_compression_capacity,
    section_moment_capacity, section_plates_bending,
)


# ---------------------------------------------------------------- compactness (cl 5.2)
def test_250ub37_flange_compact_for_fy_320():
    s = get_section("250UB37.3")
    assert s.fy_flange == 320
    plates = {p.element: p for p in section_plates_bending(s)}
    fl = plates["flange outstand"]
    # b = (bf − tw)/2 = 69.8, λe = (69.8/10.9)·√(320/250) = 7.24 < λep = 9  (Table 5.2, HR outstand)
    assert math.isclose(fl.lam_e, (69.8 / 10.9) * math.sqrt(320 / 250), rel_tol=1e-6)
    assert fl.klass == "compact" and plates["web"].klass == "compact"
    assert section_moment_capacity(s).compactness == "compact"


def test_thin_plate_outstand_is_slender():
    p = classify_plate(b=200, t=5, fy=320, element="outstand", residual="HR")
    assert p.lam_e > p.lam_ey and p.klass == "slender"
    # and a stocky one is compact
    assert classify_plate(b=50, t=10, fy=320, element="outstand").klass == "compact"


# ---------------------------------------------------------------- hand check, 250 PFC 4.0 m
def test_hand_check_250pfc_simply_supported():
    s = get_section("250 PFC")
    a = beam_actions(4000, w_G_kNm=6, w_Q_kNm=4)
    # 1.2·6 + 1.5·4 = 13.2 kN/m → M* = 13.2·4²/8 = 26.4 kNm, V* = 13.2·4/2 = 26.4 kN
    assert math.isclose(a.M_star_kNm, 26.4, rel_tol=1e-4)
    assert math.isclose(a.V_star_kN, 26.4, rel_tol=1e-4)
    assert math.isclose(a.M_serv_short_kNm, (6 + 0.7 * 4) * 16 / 8, rel_tol=1e-6)
    assert math.isclose(a.M_serv_long_kNm, (6 + 0.4 * 4) * 16 / 8, rel_tol=1e-6)

    sm = section_moment_capacity(s)
    assert sm.compactness == "compact"
    phi_fy_Sx = CONSTANTS["phi_bending"] * s.fy * s.Sx / 1e6
    assert abs(sm.phiMs_kNm - phi_fy_Sx) / phi_fy_Sx < 0.05

    # deflection 5wL⁴/384EI against a direct computation
    E, L = CONSTANTS["E"], 4000.0
    dG, dQ = a.deflections_mm(s.Ix)
    assert math.isclose(dG, 5 * 6.0 * L**4 / (384 * E * s.Ix), rel_tol=1e-9)
    assert math.isclose(dQ, 5 * 4.0 * L**4 / (384 * E * s.Ix), rel_tol=1e-9)


def test_point_load_and_two_span_formulas():
    # central point load: M* = 1.2·P·L/4
    a = beam_actions(4000, 0, 0, point_loads=[(2000, 10, 0)])
    assert math.isclose(a.M_star_kNm, 1.2 * 10 * 4 / 4, rel_tol=1e-6)
    assert math.isclose(a.V_star_kN, 1.2 * 10 / 2, rel_tol=1e-6)
    assert math.isclose(a.delta_G_x_EI, 10e3 * 4000**3 / 48, rel_tol=1e-9)   # PL³/48EI
    # two-span: M = wL²/8 at the central support, V = 5wL/8
    b = beam_actions(3000, 5, 3, support="continuous_2span")
    wu = 1.2 * 5 + 1.5 * 3
    assert math.isclose(b.M_star_kNm, wu * 9 / 8, rel_tol=1e-6)
    assert math.isclose(b.V_star_kN, 5 * wu * 3 / 8, rel_tol=1e-6)
    with pytest.raises(ValueError):
        beam_actions(3000, 5, 3, point_loads=[(1000, 1, 1)], support="continuous_2span")


# ---------------------------------------------------------------- selection
def test_design_beam_tighter_deflection_picks_heavier():
    loose = design_beam("UB", 5000, 8, 6, deflection_limit="span/250")
    tight = design_beam("UB", 5000, 8, 6, deflection_limit="span/360, max 10 mm")
    pick = lambda r: next(x for x in r if x.selected)
    assert len(loose) == len(load_catalogue()["UB"])
    assert [x.designation for x in loose] == [x.designation for x in tight]       # same walk order
    assert sum(x.selected for x in loose) == 1 and sum(x.selected for x in tight) == 1
    assert pick(tight).mass_kg_m > pick(loose).mass_kg_m
    assert pick(tight).checks[2].capacity == 10.0                                 # "max 10 mm" governs span/360 = 13.9
    assert all("section properties unverified" in x.trace[1] for x in loose if not x.verified)


def test_design_beam_segment_restraint_lowers_capacity():
    full = design_beam("PFC", 4000, 6, 4, restraint="full", include_self_weight=False)
    seg = design_beam("PFC", 4000, 6, 4, restraint="segment", include_self_weight=False)
    for f, g in zip(full, seg):
        bf = next(c for c in f.checks if c.name == "bending")
        bg = next(c for c in g.checks if c.name == "bending")
        assert bg.capacity < bf.capacity
        assert bf.demand == bg.demand


def test_parse_deflection_limit():
    assert parse_deflection_limit("span/300").limit_mm(3000) == 10.0
    d = parse_deflection_limit("span/360, max 10 mm")
    assert d.ratio == 360 and d.max_mm == 10 and d.limit_mm(5400) == 10.0 and d.limit_mm(1800) == 5.0
    with pytest.raises(ValueError):
        parse_deflection_limit("no limit")


# ---------------------------------------------------------------- columns (cl 6, cl 8)
def test_shs_column_89x5_pinned_2700():
    s = get_section("89x89x5 SHS")
    sc = section_compression_capacity(s)
    mc = member_compression_capacity(s, 2700, ke=1.0)
    assert sc.kf == 1.0 and sc.phiNs_kN > 0
    assert 0 < mc.phiNc_kN < sc.phiNs_kN
    assert math.isclose(sc.phiNs_kN, 0.9 * 1590 * 350 / 1e3, rel_tol=1e-9)
    assert mc.alpha_b == -0.5
    # utilisation rises with length
    u = [check_column(s, 150, L).utilisation for L in (1800, 2700, 3600)]
    assert u[0] < u[1] < u[2]
    # an eccentricity adds combined-action checks and increases utilisation
    cc = check_column(s, 150, 2700, eccentricity_mm=25)
    names = {c.name for c in cc.checks}
    assert {"axial_section", "axial_member", "combined_section", "combined_member"} <= names
    assert cc.utilisation > check_column(s, 150, 2700).utilisation
    assert "section properties unverified" in cc.trace[1]


def test_design_column_marks_first_adequate():
    res = design_column("SHS", 200, 3000)
    assert sum(r.selected for r in res) == 1
    sel = next(r for r in res if r.selected)
    assert sel.adequate and all(not r.adequate for r in res[: res.index(sel)])


# ---------------------------------------------------------------- catalogue
def test_catalogue_self_consistency():
    issues, n, n_verified = check_sections.run(verbose=False)
    assert n >= 14
    # verified sections must pass; unverified ones are reported, not failed
    assert not [i for i in issues if i.verified], [str(i) for i in issues if i.verified]
    for i in issues:
        assert not i.verified
    # every unverified row says so in its source
    for fam in load_catalogue().values():
        for s in fam:
            if not s.verified:
                assert "verify" in s.source.lower()


def test_catalogue_fy_by_thickness():
    for s in load_catalogue()["UB"] + load_catalogue()["PFC"]:
        exp = 320 if s.tf <= 11 else 300 if s.tf <= 17 else 280
        assert s.fy_flange == exp, s.designation
    for s in load_catalogue()["SHS"]:
        assert s.fy == 350
