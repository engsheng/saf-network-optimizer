#!/usr/bin/env python3
"""End-to-end runner for the SAF supply-chain optimizer.

Usage
-----
    python run.py                              # defaults (expects files in data/)
    python run.py --biomass data/biomass.csv --out results.json
    python run.py --no-airport-candidates     # 80 biomass sites only (ablation)

Exit codes
----------
    0  solved to proven optimality, all validation checks pass
    1  solved but at least one validation check flagged an issue
    2  solver returned no incumbent, or infeasible
    3  input-file error
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pulp

from saf import (
    build_model,
    extract_results,
    insight_warnings,
    load_airports,
    load_biomass,
    load_facilities,
    load_transport,
    print_summary,
    synthetic_biomass,
    validate_solution,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Minimise annualised SAF supply chain cost in Kalimantan.",
    )
    p.add_argument("--airports", default="data/airports.csv",
                   help="4-airport CSV (default: data/airports.csv)")
    p.add_argument("--params", default="data/parameters.xlsx",
                   help="facilities + transport + assumptions XLSX")
    p.add_argument("--biomass", default="data/biomass.csv",
                   help="80-site biomass CSV (uses synthetic layout if missing)")
    p.add_argument("--no-airport-candidates", action="store_true",
                   help="exclude the 4 airports from the candidate site set "
                        "(ablation study)")
    p.add_argument("--time-limit", type=int, default=600,
                   help="solver time limit in seconds (default: 600)")
    p.add_argument("--mip-gap", type=float, default=0.001,
                   help="MIP relative gap tolerance (default: 0.001 = 0.1%%)")
    p.add_argument("--out", default="results.json",
                   help="where to write the results JSON (default: results.json)")
    p.add_argument("--solver", default="auto", choices=("auto", "highs", "cbc"),
                   help="solver backend (default: auto -- HiGHS if available, "
                        "else bundled CBC)")
    p.add_argument("--threads", type=int, default=0,
                   help="solver thread count (0 = solver default; HiGHS only)")
    p.add_argument("--quiet", action="store_true",
                   help="suppress solver log")
    return p.parse_args()


def pick_solver(choice: str, time_limit: int, gap: float, msg: bool, threads: int = 0):
    """Return a PuLP solver instance. HiGHS is faster; CBC ships with PuLP."""
    if choice in ("auto", "highs"):
        try:
            kwargs = dict(msg=msg, timeLimit=time_limit, gapRel=gap)
            if threads > 0:
                kwargs["threads"] = threads
            s = pulp.HiGHS(**kwargs)
            if s.available():
                return s, "HiGHS"
        except Exception:
            pass
        if choice == "highs":
            raise RuntimeError(
                "HiGHS requested but not available. Install `highspy` or use --solver cbc."
            )
    s = pulp.PULP_CBC_CMD(msg=msg, timeLimit=time_limit, gapRel=gap)
    if not s.available():
        raise RuntimeError("No usable solver found (CBC should ship with PuLP)")
    return s, "CBC"


def main() -> int:
    args = parse_args()

    # ------------------------------------------------------------------
    # Load inputs
    # ------------------------------------------------------------------
    airports_path = Path(args.airports)
    params_path = Path(args.params)
    biomass_path = Path(args.biomass)

    if not airports_path.exists():
        print(f"ERROR: airports file not found: {airports_path}", file=sys.stderr)
        return 3
    if not params_path.exists():
        print(f"ERROR: parameters file not found: {params_path}", file=sys.stderr)
        return 3

    try:
        airports = load_airports(airports_path)
        facilities = load_facilities(params_path)
        transport = load_transport(params_path)
    except Exception as e:
        print(f"ERROR loading inputs: {e}", file=sys.stderr)
        return 3

    depot_specs = [f for f in facilities if f.facility.lower() == "depot"]
    refinery_specs = [f for f in facilities if f.facility.lower() == "refinery"]
    if len(depot_specs) < 2 or len(refinery_specs) < 2:
        print(f"ERROR: expected 2 depot sizes and 2 refinery sizes, "
              f"got {len(depot_specs)} / {len(refinery_specs)}",
              file=sys.stderr)
        return 3

    if biomass_path.exists():
        farms = load_biomass(biomass_path)
        print(f"[inputs] loaded {len(farms)} farms from {biomass_path}")
    else:
        print(f"[inputs] WARNING: {biomass_path} not found; using SYNTHETIC "
              f"80-farm layout. Download the real biomass file from the "
              f"organizer's Drive for an actual submission.")
        farms = synthetic_biomass(n=80, total_kt=6800.0)

    total_biomass = sum(f.biomass_kt for f in farms)
    total_demand_bio = sum(a.demand_kt for a in airports) / 0.20  # 1,200 / 0.20 = 6,000
    print(f"[inputs] {len(airports)} airports; total SAF demand = "
          f"{sum(a.demand_kt for a in airports):.0f} kt/yr "
          f"(requires {total_demand_bio:.0f} kt biomass at 0.20 yield)")
    print(f"[inputs] total biomass available = {total_biomass:.0f} kt/yr "
          f"({'OK' if total_biomass >= total_demand_bio else 'INFEASIBLE: too little'})")

    # ------------------------------------------------------------------
    # Build candidate site set
    # ------------------------------------------------------------------
    candidates = [(f.farm_id, f.lat, f.lon) for f in farms]
    if not args.no_airport_candidates:
        candidates.extend([(f"AIR_{a.iata}", a.lat, a.lon) for a in airports])
    print(f"[model]  {len(candidates)} candidate sites "
          f"({len(farms)} biomass + {len(candidates) - len(farms)} airports)")

    # ------------------------------------------------------------------
    # Build and solve
    # ------------------------------------------------------------------
    t0 = time.time()
    built = build_model(
        farms=farms, airports=airports,
        depot_specs=depot_specs, refinery_specs=refinery_specs,
        transport=transport, candidate_sites=candidates,
    )
    build_s = time.time() - t0
    model = built["model"]
    print(f"[model]  built in {build_s:.1f}s "
          f"({model.numVariables():,} vars, {model.numConstraints():,} constraints)")

    solver, solver_name = pick_solver(
        args.solver, args.time_limit, args.mip_gap,
        msg=not args.quiet, threads=args.threads,
    )
    print(f"[solve]  {solver_name}, time limit {args.time_limit}s, "
          f"gap {args.mip_gap:.3%}")
    t0 = time.time()
    status = model.solve(solver)
    solve_s = time.time() - t0
    status_name = pulp.LpStatus[status]
    print(f"[solve]  finished in {solve_s:.1f}s, status: {status_name}")

    # ------------------------------------------------------------------
    # Interpret solver status
    # ------------------------------------------------------------------
    obj = pulp.value(model.objective)
    if status == pulp.LpStatusInfeasible:
        print("ERROR: model is infeasible. Check supply vs demand "
              "and farm/depot/refinery capacities.", file=sys.stderr)
        return 2
    if obj is None:
        print(f"ERROR: solver status {status_name!r} and no incumbent "
              f"solution available.", file=sys.stderr)
        return 2
    if status != pulp.LpStatusOptimal:
        print(f"[solve]  WARNING: status is '{status_name}', not 'Optimal'. "
              f"Reporting incumbent (may be sub-optimal).")

    # ------------------------------------------------------------------
    # Report and validate
    # ------------------------------------------------------------------
    print_summary(built, farms, airports)
    results = extract_results(built, farms, airports)
    results["meta"] = {
        "status": status_name,
        "solver": solver_name,
        "build_seconds": build_s,
        "solve_seconds": solve_s,
        "n_farms": len(farms),
        "n_candidates": len(candidates),
        "n_airport_candidates": sum(1 for c in candidates if c[0].startswith("AIR_")),
        "biomass_file": str(biomass_path),
        "synthetic_biomass": not biomass_path.exists(),
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\n[output] wrote {out_path}")

    issues = validate_solution(built, farms, airports)
    warnings = insight_warnings(built, airports)

    if warnings:
        print("\n[check]  INSIGHT WARNINGS (design doc predictions diverge):")
        for w in warnings:
            print(f"  ! {w}")

    if issues:
        print("\n[check]  HARD VALIDATION ISSUES (solution violates problem rules):")
        for i in issues:
            print(f"  - {i}")
        return 1

    print("\n[check]  all hard validation checks passed"
          + (f" ({len(warnings)} insight warnings)" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
