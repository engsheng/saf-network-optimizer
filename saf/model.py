"""Build the SAF supply-chain MILP (PuLP + HiGHS).

Problem shape (three-echelon network: farms -> depots -> refineries -> airports)
-------------------------------------------------------------------------------

Candidate locations : the 80 biomass sites + the 4 airports (84 points).
                      Depots and refineries may open at any candidate.

Decision variables
------------------
  y_dep[j, s_D]     binary   open a depot of size s_D at candidate j
  y_ref[j, s_R]     binary   open a refinery of size s_R at candidate j
  x[f, j, s_D]      >= 0     biomass flow (kt/yr) farm f -> depot j of size s_D
  w[d, r, s_R]      >= 0     biomass flow (kt/yr) depot d -> refinery r of size s_R
  z[r, a, s_R]      >= 0     SAF flow (kt/yr) refinery r of size s_R -> airport a

Size-indexing on x and w keeps the per-tonne variable OpEx term LINEAR (the
rate differs by size, 5 vs 4 for depots and 60 vs 55 for refineries; see the
Modeling Assumptions doc, "Variable OpEx").

Objective (USD/yr)
------------------
  min  annualised_CapEx + fixed_OpEx + variable_OpEx + logistics

Constraints
-----------
  (1) farm supply (soft):  sum_j sum_s x[f,j,s] <= biomass_f
  (2) one size per site:   sum_s y_dep[j,s] <= 1   ;   sum_s y_ref[j,s] <= 1
  (3) depot capacity:      sum_f x[f,d,s] <= cap_dep[s] * y_dep[d,s]
  (4) depot mass balance:  sum_f sum_s x[f,d,s] = sum_r sum_s' w[d,r,s']   (lossless)
  (5) refinery capacity:   sum_d w[d,r,s] <= cap_ref[s] * y_ref[r,s]
  (6) refinery yield:      0.20 * sum_d w[d,r,s] = sum_a z[r,a,s]
  (7) airport demand (eq): sum_r sum_s z[r,a,s] = demand_a

Flow units are kt/yr. We multiply by 1000 (t per kt) only where per-tonne
costs are applied, so the solver sees costs in USD/yr consistently.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import pulp

from .data import YIELD, Airport, FacilitySpec, Farm, TransportLeg
from .distance import road_km


T_PER_KT = 1000.0  # tonnes per kilotonne

CandidateSite = Tuple[str, float, float]  # (site_id, lat, lon)


def build_model(
    farms: List[Farm],
    airports: List[Airport],
    depot_specs: List[FacilitySpec],
    refinery_specs: List[FacilitySpec],
    transport: Dict[str, TransportLeg],
    candidate_sites: List[CandidateSite],
    name: str = "SAF_Network",
) -> Dict:
    """Build and return a dict with the PuLP model + variable handles.

    Keys returned:
      model, y_dep, y_ref, x, w, z, distances, cost_components,
      candidates, cand_of, sizes
    """
    # ------------------------------------------------------------------
    # Index sets and lookup maps
    # ------------------------------------------------------------------
    FARMS = [f.farm_id for f in farms]
    farm_of = {f.farm_id: f for f in farms}
    CANDS = [c[0] for c in candidate_sites]
    cand_of: Dict[str, Tuple[float, float]] = {c[0]: (c[1], c[2]) for c in candidate_sites}
    AIRPORTS = [a.iata for a in airports]
    air_of = {a.iata: a for a in airports}

    DEP_SIZES = [s.size for s in depot_specs]
    dep_spec = {s.size: s for s in depot_specs}
    REF_SIZES = [s.size for s in refinery_specs]
    ref_spec = {s.size: s for s in refinery_specs}

    # ------------------------------------------------------------------
    # Pre-compute distances (km). All legs use haversine * 1.3.
    # The barge leg to TRK uses the same formula; the per-tonne-km rate
    # is identical (0.08 USD/t-km), so arithmetic is uniform.
    # ------------------------------------------------------------------
    d_fd: Dict[Tuple[str, str], float] = {}
    for f in farms:
        for c in CANDS:
            lat2, lon2 = cand_of[c]
            d_fd[(f.farm_id, c)] = road_km(f.lat, f.lon, lat2, lon2)

    d_dr: Dict[Tuple[str, str], float] = {}
    for d in CANDS:
        la1, lo1 = cand_of[d]
        for r in CANDS:
            la2, lo2 = cand_of[r]
            d_dr[(d, r)] = road_km(la1, lo1, la2, lo2)

    d_ra: Dict[Tuple[str, str], float] = {}
    for r in CANDS:
        la1, lo1 = cand_of[r]
        for a in airports:
            d_ra[(r, a.iata)] = road_km(la1, lo1, a.lat, a.lon)

    # ------------------------------------------------------------------
    # Decision variables
    # ------------------------------------------------------------------
    model = pulp.LpProblem(name, pulp.LpMinimize)

    y_dep = pulp.LpVariable.dicts("y_dep", (CANDS, DEP_SIZES), cat=pulp.LpBinary)
    y_ref = pulp.LpVariable.dicts("y_ref", (CANDS, REF_SIZES), cat=pulp.LpBinary)
    x = pulp.LpVariable.dicts("x", (FARMS, CANDS, DEP_SIZES), lowBound=0)
    w = pulp.LpVariable.dicts("w", (CANDS, CANDS, REF_SIZES), lowBound=0)
    z = pulp.LpVariable.dicts("z", (CANDS, AIRPORTS, REF_SIZES), lowBound=0)

    # Transport rates (USD per tonne-km); same for both truck sizes.
    c_fd = transport["FD"].cost_usd_per_tkm
    c_dr = transport["DR"].cost_usd_per_tkm
    c_ra = transport["RA"].cost_usd_per_tkm  # also used for TRK barge

    # ------------------------------------------------------------------
    # Objective components (each returned separately so we can report
    # the breakdown and so judges can audit the cost build-up).
    # ------------------------------------------------------------------
    annualised_capex = (
        pulp.lpSum(dep_spec[s].annualised_capex_usd * y_dep[c][s]
                   for c in CANDS for s in DEP_SIZES)
        + pulp.lpSum(ref_spec[s].annualised_capex_usd * y_ref[c][s]
                     for c in CANDS for s in REF_SIZES)
    )
    fixed_opex = (
        pulp.lpSum(dep_spec[s].fixed_opex_usd * y_dep[c][s]
                   for c in CANDS for s in DEP_SIZES)
        + pulp.lpSum(ref_spec[s].fixed_opex_usd * y_ref[c][s]
                     for c in CANDS for s in REF_SIZES)
    )
    variable_opex_dep = pulp.lpSum(
        dep_spec[s].variable_opex_usd_per_t * T_PER_KT * x[f.farm_id][c][s]
        for f in farms for c in CANDS for s in DEP_SIZES
    )
    variable_opex_ref = pulp.lpSum(
        ref_spec[sr].variable_opex_usd_per_t * T_PER_KT * w[d][r][sr]
        for d in CANDS for r in CANDS for sr in REF_SIZES
    )
    logistics_fd = pulp.lpSum(
        c_fd * T_PER_KT * d_fd[(f.farm_id, c)] * x[f.farm_id][c][s]
        for f in farms for c in CANDS for s in DEP_SIZES
    )
    logistics_dr = pulp.lpSum(
        c_dr * T_PER_KT * d_dr[(d, r)] * w[d][r][sr]
        for d in CANDS for r in CANDS for sr in REF_SIZES
    )
    logistics_ra = pulp.lpSum(
        c_ra * T_PER_KT * d_ra[(r, a.iata)] * z[r][a.iata][sr]
        for r in CANDS for a in airports for sr in REF_SIZES
    )
    logistics = logistics_fd + logistics_dr + logistics_ra

    model += (annualised_capex + fixed_opex
              + variable_opex_dep + variable_opex_ref + logistics), "total_annual_cost"

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    # (1) Farm supply (soft upper bound) -- a farm may be partially used or skipped.
    for f in farms:
        model += (
            pulp.lpSum(x[f.farm_id][c][s] for c in CANDS for s in DEP_SIZES)
            <= f.biomass_kt,
            f"supply_{f.farm_id}",
        )

    # (2) At most one size per candidate (for each facility type).
    for c in CANDS:
        model += (pulp.lpSum(y_dep[c][s] for s in DEP_SIZES) <= 1,
                  f"one_dep_size_{c}")
        model += (pulp.lpSum(y_ref[c][s] for s in REF_SIZES) <= 1,
                  f"one_ref_size_{c}")

    # (3) Depot capacity binds flow to a chosen size.
    for c in CANDS:
        for s in DEP_SIZES:
            model += (
                pulp.lpSum(x[f.farm_id][c][s] for f in farms)
                <= dep_spec[s].capacity_kt * y_dep[c][s],
                f"cap_dep_{c}_{s}",
            )

    # (4) Depot mass balance -- lossless (tonnes in = tonnes out).
    #     Summed over sizes at each candidate (at most one is non-zero anyway).
    for d in CANDS:
        model += (
            pulp.lpSum(x[f.farm_id][d][s] for f in farms for s in DEP_SIZES)
            == pulp.lpSum(w[d][r][sr] for r in CANDS for sr in REF_SIZES),
            f"mb_dep_{d}",
        )

    # (5) Refinery capacity on biomass intake.
    for r in CANDS:
        for sr in REF_SIZES:
            model += (
                pulp.lpSum(w[d][r][sr] for d in CANDS)
                <= ref_spec[sr].capacity_kt * y_ref[r][sr],
                f"cap_ref_{r}_{sr}",
            )

    # (6) Refinery yield: 0.20 * biomass in = SAF out.
    for r in CANDS:
        for sr in REF_SIZES:
            model += (
                YIELD * pulp.lpSum(w[d][r][sr] for d in CANDS)
                == pulp.lpSum(z[r][a.iata][sr] for a in airports),
                f"yield_{r}_{sr}",
            )

    # (7) Airport demand (hard equality).
    for a in airports:
        model += (
            pulp.lpSum(z[r][a.iata][sr] for r in CANDS for sr in REF_SIZES)
            == a.demand_kt,
            f"demand_{a.iata}",
        )

    return {
        "model": model,
        "y_dep": y_dep,
        "y_ref": y_ref,
        "x": x,
        "w": w,
        "z": z,
        "distances": {"fd": d_fd, "dr": d_dr, "ra": d_ra},
        "cost_components": {
            "annualised_capex": annualised_capex,
            "fixed_opex": fixed_opex,
            "variable_opex_dep": variable_opex_dep,
            "variable_opex_ref": variable_opex_ref,
            "logistics_fd": logistics_fd,
            "logistics_dr": logistics_dr,
            "logistics_ra": logistics_ra,
        },
        "candidates": CANDS,
        "cand_of": cand_of,
        "sizes": {"dep": DEP_SIZES, "ref": REF_SIZES},
        "specs": {"dep": dep_spec, "ref": ref_spec},
    }
