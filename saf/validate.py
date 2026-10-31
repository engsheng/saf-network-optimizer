"""Post-solve validation checks.

Mirrors the "Validation checks" section of the Design Approach doc. Every
check except the last is a hard assertion against the problem definition;
the last is an insight sanity check (no refinery at an airport candidate)
that can be reasonably violated in corner cases but should prompt a review.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pulp

from .data import YIELD, Airport, Farm


def _val(v) -> float:
    """Safe value extraction (treats None as 0)."""
    val = pulp.value(v)
    return 0.0 if val is None else float(val)


def validate_solution(
    built: Dict[str, Any],
    farms: List[Farm],
    airports: List[Airport],
    tol: float = 1e-3,
) -> List[str]:
    """Return a list of issue strings. Empty list = all checks passed.

    Tolerances are in kt (flows are in kt/yr); 1e-3 kt = 1 tonne.
    """
    issues: List[str] = []
    CANDS: List[str] = built["candidates"]
    DEP_SIZES: List[str] = built["sizes"]["dep"]
    REF_SIZES: List[str] = built["sizes"]["ref"]
    x, w, z = built["x"], built["w"], built["z"]
    y_dep, y_ref = built["y_dep"], built["y_ref"]

    # (A) Flow non-negativity. LpVariable lowBound=0 should guarantee this,
    #     but a numerical check costs nothing and catches solver quirks.
    for f in farms:
        for c in CANDS:
            for s in DEP_SIZES:
                v = _val(x[f.farm_id][c][s])
                if v < -tol:
                    issues.append(f"negative x[{f.farm_id},{c},{s}] = {v:.6f}")
    for d in CANDS:
        for r in CANDS:
            for sr in REF_SIZES:
                v = _val(w[d][r][sr])
                if v < -tol:
                    issues.append(f"negative w[{d},{r},{sr}] = {v:.6f}")
    for r in CANDS:
        for a in airports:
            for sr in REF_SIZES:
                v = _val(z[r][a.iata][sr])
                if v < -tol:
                    issues.append(f"negative z[{r},{a.iata},{sr}] = {v:.6f}")

    # (B) Farm supply respected.
    for f in farms:
        used = sum(_val(x[f.farm_id][c][s]) for c in CANDS for s in DEP_SIZES)
        if used > f.biomass_kt + tol:
            issues.append(
                f"supply violated at farm {f.farm_id}: used {used:.4f} > avail {f.biomass_kt:.4f}"
            )

    # (C) Depot mass balance (lossless): intake == outflow at every candidate.
    for d in CANDS:
        inflow = sum(_val(x[f.farm_id][d][s]) for f in farms for s in DEP_SIZES)
        outflow = sum(_val(w[d][r][sr]) for r in CANDS for sr in REF_SIZES)
        if abs(inflow - outflow) > tol:
            issues.append(
                f"depot {d} mass imbalance: in={inflow:.4f}, out={outflow:.4f}"
            )

    # (D) Refinery yield check: 0.20 * biomass intake = SAF outflow.
    for r in CANDS:
        for sr in REF_SIZES:
            in_bio = sum(_val(w[d][r][sr]) for d in CANDS)
            out_saf = sum(_val(z[r][a.iata][sr]) for a in airports)
            expected = YIELD * in_bio
            if abs(out_saf - expected) > tol:
                issues.append(
                    f"yield violated at {r}/{sr}: expected 0.20*{in_bio:.4f}"
                    f"={expected:.4f}, got {out_saf:.4f}"
                )

    # (E) Capacity respected at every open facility.
    for c in CANDS:
        for s in DEP_SIZES:
            if _val(y_dep[c][s]) > 0.5:
                thru = sum(_val(x[f.farm_id][c][s]) for f in farms)
                cap = built["specs"]["dep"][s].capacity_kt
                if thru > cap + tol:
                    issues.append(
                        f"depot {c} size {s} over capacity: {thru:.4f} > {cap:.4f}"
                    )
        for sr in REF_SIZES:
            if _val(y_ref[c][sr]) > 0.5:
                thru = sum(_val(w[d][c][sr]) for d in CANDS)
                cap = built["specs"]["ref"][sr].capacity_kt
                if thru > cap + tol:
                    issues.append(
                        f"refinery {c} size {sr} over capacity: {thru:.4f} > {cap:.4f}"
                    )

    # (F) Airport demand met exactly.
    for a in airports:
        got = sum(_val(z[r][a.iata][sr]) for r in CANDS for sr in REF_SIZES)
        if abs(got - a.demand_kt) > tol:
            issues.append(
                f"demand violated at {a.iata}: got {got:.4f}, need {a.demand_kt:.4f}"
            )

    return issues


def insight_warnings(
    built: Dict[str, Any],
    airports: List[Airport],
) -> List[str]:
    """Design-doc insight check: no refinery at an airport candidate.

    The 7.5x cost asymmetry argument predicts refineries belong near depot
    clusters, NOT at airports. If a refinery lands at an airport candidate,
    the model is telling you the insight does NOT hold for this instance --
    worth a closer look at the biomass distribution, but not a solver error.
    """
    warnings: List[str] = []
    CANDS: List[str] = built["candidates"]
    REF_SIZES: List[str] = built["sizes"]["ref"]
    y_ref = built["y_ref"]
    for c in CANDS:
        if c.startswith("AIR_"):
            for sr in REF_SIZES:
                if _val(y_ref[c][sr]) > 0.5:
                    warnings.append(
                        f"refinery opened at airport candidate {c} (size {sr}); "
                        f"the 7.5x insight predicts this should be rare -- "
                        f"inspect the biomass distribution around {c[4:]}"
                    )
    return warnings
