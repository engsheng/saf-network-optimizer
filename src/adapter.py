"""
Adapter between the saf-package MILP and the dashboard Solution schema.

- solve(): run the real MILP (HiGHS -> CBC fallback), return a Solution dict.
- translate(): convert an existing extract_results() dict to the Solution dict.
- load_cached_solution() / save_solution(): persist the latest run so the
  dashboard starts instantly instead of waiting a minute for the solver.

The saf package's own `extract_results` emits a different, slightly richer
shape (cost broken down by leg, no explicit `facilities` list, flows that
refer to candidate IDs rather than (type, candidate) IDs). This module
rewrites it into the shape the viz code reads.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pulp

from saf import (
    YIELD, CAPEX_YEARS,
    build_model, extract_results,
    insight_warnings, validate_solution as validate_milp,
    mip_gap_rel, optimality_status,
    load_airports as load_airports_saf,
    load_biomass as load_biomass_saf,
    load_facilities, load_transport,
    synthetic_biomass,
)

T_PER_KT = 1000.0

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CACHE_PATH = DATA_DIR / "cached_solution.json"


# ---------------------------------------------------------------------------
# Transport rates (loaded lazily, cached module-level after first use)
# ---------------------------------------------------------------------------

_RATES: Dict[str, float] | None = None

def _rates() -> Dict[str, float]:
    """USD per tonne-km, one per leg. Also used for the TRK barge leg."""
    global _RATES
    if _RATES is None:
        legs = load_transport(DATA_DIR / "parameters.xlsx")
        _RATES = {k: v.cost_usd_per_tkm for k, v in legs.items()}
    return _RATES


# ---------------------------------------------------------------------------
# Solver pick
# ---------------------------------------------------------------------------

def pick_solver(time_limit: int = 120, gap: float = 0.001, msg: bool = False):
    """HiGHS if available, else bundled CBC. Returns (solver, name)."""
    try:
        s = pulp.HiGHS(msg=msg, timeLimit=time_limit, gapRel=gap)
        if s.available():
            return s, "HiGHS"
    except Exception:
        pass
    return pulp.PULP_CBC_CMD(msg=msg, timeLimit=time_limit, gapRel=gap), "CBC"


# ---------------------------------------------------------------------------
# Translate the saf-package extract_results() dict → dashboard Solution
# ---------------------------------------------------------------------------

def _facility_display_id(role: str, candidate: str) -> str:
    """
    Unique facility id used by the dashboard. Depot/refinery can both open
    at the same candidate; prefixing the role keeps them distinct.
    role is 'D' or 'R'.
    """
    return f"{role}-{candidate}"


def translate(
    results: Dict[str, Any],
    *,
    airports_df=None,
    status_name: str = "optimal",
    solver_name: str = "unknown",
    solve_time_sec: float | None = None,
    optimality_gap: float | None = None,
    throughput_tol_kt: float = 0.01,
) -> Dict[str, Any]:
    """
    Convert a saf.report.extract_results() dict into the dashboard Solution
    schema (see src/schema.py).

    `results` is the output of `extract_results(built, farms, airports)` or an
    equivalent JSON file written by `run.py`.
    """
    rates = _rates()
    c_FD, c_DR, c_RA = rates["FD"], rates["DR"], rates["RA"]

    # ---------- Facilities ----------
    # Depot & refinery CapEx and OpEx by size, from the facility specs.
    facilities_specs = load_facilities(DATA_DIR / "parameters.xlsx")
    dep_spec = {s.size: s for s in facilities_specs if s.facility.lower() == "depot"}
    ref_spec = {s.size: s for s in facilities_specs if s.facility.lower() == "refinery"}

    facilities: List[Dict[str, Any]] = []

    for d in results["depots_opened"]:
        thru_kt = d["throughput_kt_biomass"]
        if thru_kt < throughput_tol_kt:
            continue  # phantom (binary = 1 but zero flow); hide from the map
        spec = dep_spec[d["size"]]
        facilities.append({
            "id": _facility_display_id("D", d["site_id"]),
            "type": "depot",
            "size": d["size"],
            "lat": d["lat"], "lon": d["lon"],
            "capacity_kt_in": spec.capacity_kt,
            "throughput_kt_in": thru_kt,
            "utilisation": min(1.0, thru_kt / spec.capacity_kt),
            "annualised_capex_usd": spec.annualised_capex_usd,
            "fixed_opex_usd": spec.fixed_opex_usd,
            "variable_opex_usd": thru_kt * T_PER_KT * spec.variable_opex_usd_per_t,
        })

    for r in results["refineries_opened"]:
        intake_kt = r["intake_kt_biomass"]
        if intake_kt < throughput_tol_kt:
            continue
        spec = ref_spec[r["size"]]
        facilities.append({
            "id": _facility_display_id("R", r["site_id"]),
            "type": "refinery",
            "size": r["size"],
            "lat": r["lat"], "lon": r["lon"],
            "capacity_kt_in": spec.capacity_kt,
            "throughput_kt_in": intake_kt,
            "utilisation": min(1.0, intake_kt / spec.capacity_kt),
            "annualised_capex_usd": spec.annualised_capex_usd,
            "fixed_opex_usd": spec.fixed_opex_usd,
            "variable_opex_usd": intake_kt * T_PER_KT * spec.variable_opex_usd_per_t,
        })

    # ---------- Flows ----------
    flows: List[Dict[str, Any]] = []

    # farm_to_depot: from biomass site -> D-<candidate>
    for f in results["flows_farm_to_depot"]:
        if f["flow_kt"] < throughput_tol_kt:
            continue
        tonnes = f["flow_kt"] * T_PER_KT
        flows.append({
            "from_id": f["farm"],
            "to_id": _facility_display_id("D", f["depot"]),
            "stage": "farm_to_depot",
            "tonnes": tonnes,
            "distance_km": f["distance_km"],
            "cost_usd": tonnes * f["distance_km"] * c_FD,
            "mode": "truck",
        })

    # depot_to_refinery
    for f in results["flows_depot_to_refinery"]:
        if f["flow_kt"] < throughput_tol_kt:
            continue
        tonnes = f["flow_kt"] * T_PER_KT
        flows.append({
            "from_id": _facility_display_id("D", f["depot"]),
            "to_id": _facility_display_id("R", f["refinery"]),
            "stage": "depot_to_refinery",
            "tonnes": tonnes,
            "distance_km": f["distance_km"],
            "cost_usd": tonnes * f["distance_km"] * c_DR,
            "mode": "truck",
        })

    # refinery_to_airport (SAF; TRK uses barge at same rate)
    for f in results["flows_refinery_to_airport"]:
        if f["flow_kt"] < throughput_tol_kt:
            continue
        tonnes = f["flow_kt"] * T_PER_KT
        mode = f.get("mode", "truck")
        flows.append({
            "from_id": _facility_display_id("R", f["refinery"]),
            "to_id": f["airport"],
            "stage": "refinery_to_airport",
            "tonnes": tonnes,
            "distance_km": f["distance_km"],
            "cost_usd": tonnes * f["distance_km"] * c_RA,
            "mode": mode,
        })

    # ---------- Airport delivery ----------
    airport_delivery = []
    for a in results["airport_receipts"]:
        sources = sorted({
            _facility_display_id("R", f["refinery"])
            for f in results["flows_refinery_to_airport"]
            if f["airport"] == a["iata"] and f["flow_kt"] >= throughput_tol_kt
        })
        airport_delivery.append({
            "iata": a["iata"],
            "demand_kt": a["demand_kt"],
            "delivered_kt": a["received_kt"],
            "sources": sources,
        })

    # ---------- Cost rollup (combine saf's split keys into the dashboard's) ----------
    cb_raw = results["cost_breakdown_usd"]
    capex_annualised = cb_raw.get("annualised_capex", 0.0)
    fixed_opex = cb_raw.get("fixed_opex", 0.0)
    variable_opex = cb_raw.get("variable_opex_dep", 0.0) + cb_raw.get("variable_opex_ref", 0.0)
    # The saf package rolls barge into logistics_ra. Split by inspecting flow modes.
    barge_cost = sum(f["cost_usd"] for f in flows if f["mode"] == "barge")
    trucking_cost = sum(f["cost_usd"] for f in flows if f["mode"] == "truck")
    # Note: trucking_cost here is computed from flows, which may differ from
    # the sum of logistics_fd+logistics_dr+logistics_ra by a hair due to the
    # phantom-flow filter above. The validator will flag it if it drifts.
    total = capex_annualised + fixed_opex + variable_opex + trucking_cost + barge_cost

    # ---------- Assemble ----------
    insights = []
    try:
        # Try to surface the saf package's design-doc insight warnings
        # (if we were given a built-model handle; this path is used in solve()).
        pass
    except Exception:
        pass

    note_parts = []
    if solve_time_sec is not None:
        note_parts.append(f"Solved in {solve_time_sec:.1f}s with {solver_name}.")
    if results.get("meta", {}).get("synthetic_biomass"):
        note_parts.append(
            "Running on the SYNTHETIC 80-farm fallback — drop the real "
            "biomass CSV at data/biomass.csv for a scorable submission."
        )

    return {
        "status": "optimal" if status_name.lower() == "optimal" else status_name.lower(),
        "solve_time_sec": solve_time_sec if solve_time_sec is not None else 0.0,
        # PuLP's status says "Optimal" for time-limited incumbents too, so the
        # real verdict comes from saf.report (None = not recorded, e.g. old files).
        "optimality_status": results.get("optimality_status"),
        "optimality_gap": (optimality_gap if optimality_gap is not None
                           else results.get("mip_gap_rel")),
        "objective_value_usd": total,
        "cost_breakdown": {
            "capex_annualised_usd": capex_annualised,
            "fixed_opex_usd": fixed_opex,
            "variable_opex_usd": variable_opex,
            "trucking_usd": trucking_cost,
            "barge_usd": barge_cost,
            "total_usd": total,
        },
        "facilities": facilities,
        "flows": flows,
        "airport_delivery": airport_delivery,
        "notes": " ".join(note_parts) if note_parts else None,
    }


# ---------------------------------------------------------------------------
# Real solve
# ---------------------------------------------------------------------------

def solve(
    time_limit: int = 180,
    mip_gap: float = 0.001,
    include_airport_candidates: bool = True,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Build and solve the real MILP, return a dashboard Solution dict.

    Uses the saf package's build_model/extract_results. On first run this
    takes ~60 s with HiGHS (minutes with CBC).
    """
    t0 = time.time()

    airports = load_airports_saf(DATA_DIR / "airports.csv")
    facilities = load_facilities(DATA_DIR / "parameters.xlsx")
    transport = load_transport(DATA_DIR / "parameters.xlsx")

    depot_specs = [f for f in facilities if f.facility.lower() == "depot"]
    refinery_specs = [f for f in facilities if f.facility.lower() == "refinery"]

    biomass_csv = DATA_DIR / "biomass.csv"
    if biomass_csv.exists():
        farms = load_biomass_saf(biomass_csv)
    else:
        farms = synthetic_biomass(n=80, total_kt=6800.0)

    candidates = [(f.farm_id, f.lat, f.lon) for f in farms]
    if include_airport_candidates:
        candidates.extend([(f"AIR_{a.iata}", a.lat, a.lon) for a in airports])

    built = build_model(
        farms=farms, airports=airports,
        depot_specs=depot_specs, refinery_specs=refinery_specs,
        transport=transport, candidate_sites=candidates,
    )
    solver, solver_name = pick_solver(time_limit, mip_gap, msg=verbose)
    status = built["model"].solve(solver)
    solve_time = time.time() - t0
    status_name = pulp.LpStatus[status]

    if pulp.value(built["model"].objective) is None:
        return {
            "status": "infeasible",
            "solve_time_sec": solve_time,
            "optimality_gap": 0.0,
            "objective_value_usd": 0.0,
            "cost_breakdown": {k: 0.0 for k in
                ("capex_annualised_usd", "fixed_opex_usd", "variable_opex_usd",
                 "trucking_usd", "barge_usd", "total_usd")},
            "facilities": [],
            "flows": [],
            "airport_delivery": [{"iata": a.iata, "demand_kt": a.demand_kt,
                                   "delivered_kt": 0.0, "sources": []}
                                  for a in airports],
            "notes": f"Solver returned {status_name!r} with no incumbent.",
        }

    raw = extract_results(built, farms, airports,
                          optimality_status=optimality_status(built["model"]),
                          mip_gap_rel=mip_gap_rel(built["model"]))
    raw["meta"] = {
        "status": status_name,
        "solver": solver_name,
        "synthetic_biomass": not biomass_csv.exists(),
    }

    sol = translate(
        raw,
        status_name=status_name,
        solver_name=solver_name,
        solve_time_sec=solve_time,
    )

    # Attach the saf package's extra checks to the notes
    extra_notes = []
    issues = validate_milp(built, farms, airports)
    warnings = insight_warnings(built, airports)
    if warnings:
        extra_notes.append(f"{len(warnings)} insight warning(s): " + " | ".join(warnings))
    if issues:
        extra_notes.append(f"{len(issues)} HARD validation issue(s): " + " | ".join(issues))
    if extra_notes:
        sol["notes"] = (sol.get("notes") or "") + " " + " ".join(extra_notes)

    return sol


# ---------------------------------------------------------------------------
# Cache (so the dashboard loads instantly on cold start)
# ---------------------------------------------------------------------------

def save_solution(sol: Dict[str, Any], path: Path | None = None) -> Path:
    path = path or CACHE_PATH
    path.write_text(json.dumps(sol, indent=2))
    return path


def load_cached_solution(path: Path | None = None) -> Dict[str, Any] | None:
    path = path or CACHE_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def load_run_py_results(path: Path) -> Dict[str, Any]:
    """
    Translate a results.json produced by `python run.py` into the dashboard
    Solution shape. Useful if the user has already solved from the CLI.
    """
    raw = json.loads(Path(path).read_text())
    meta = raw.get("meta", {})
    return translate(
        raw,
        status_name=meta.get("status", "Optimal"),
        solver_name=meta.get("solver", "unknown"),
        solve_time_sec=meta.get("solve_seconds"),
    )
