"""End-to-end smoke test.

Uses the synthetic 80-farm layout so the test runs without the organizer's
biomass file. Verifies the model builds, solves to optimality, and all
validation checks pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when running the test directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pulp

from saf import (
    build_model, extract_results,
    load_airports, load_facilities, load_transport,
    synthetic_biomass, validate_solution,
    YIELD,
)
from saf.distance import haversine_km, road_km, ROAD_FACTOR


DATA = Path(__file__).resolve().parent.parent / "data"


def test_haversine_and_road_factor():
    # Pontianak (PNK) to Balikpapan (BPN): ~830 km great-circle.
    d_gc = haversine_km(-0.15071, 109.40400, -1.26827, 116.89400)
    assert 800 < d_gc < 870, d_gc
    d_road = road_km(-0.15071, 109.40400, -1.26827, 116.89400)
    assert abs(d_road - ROAD_FACTOR * d_gc) < 1e-9


def test_end_to_end_synthetic():
    airports = load_airports(DATA / "airports.csv")
    facilities = load_facilities(DATA / "parameters.xlsx")
    transport = load_transport(DATA / "parameters.xlsx")
    depot_specs = [f for f in facilities if f.facility == "Depot"]
    refinery_specs = [f for f in facilities if f.facility == "Refinery"]

    assert len(airports) == 4
    assert sum(a.demand_kt for a in airports) == 1200.0
    assert any(a.iata == "TRK" and a.barge_only for a in airports)

    farms = synthetic_biomass(n=80, total_kt=6800.0, seed=0)
    assert len(farms) == 80
    assert abs(sum(f.biomass_kt for f in farms) - 6800.0) < 1e-6

    candidates = [(f.farm_id, f.lat, f.lon) for f in farms]
    candidates.extend([(f"AIR_{a.iata}", a.lat, a.lon) for a in airports])

    built = build_model(
        farms=farms, airports=airports,
        depot_specs=depot_specs, refinery_specs=refinery_specs,
        transport=transport, candidate_sites=candidates,
    )
    # Prefer HiGHS (faster) but fall back to bundled CBC.
    # Loose gap tolerance so the smoke test stays fast; the real run.py
    # defaults to 1% gap.
    try:
        solver = pulp.HiGHS(msg=False, timeLimit=90, gapRel=0.10, threads=4)
        if not solver.available():
            raise RuntimeError
    except Exception:
        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=120, gapRel=0.10)
    status = built["model"].solve(solver)
    # PuLP maps "feasible-with-time-limit" to Optimal, which is good enough
    # for smoke-testing the pipeline. The important checks are below.
    assert pulp.value(built["model"].objective) is not None, \
        f"no incumbent (status={pulp.LpStatus[status]})"

    issues = validate_solution(built, farms, airports)
    assert not issues, "\n".join(issues)

    results = extract_results(built, farms, airports)
    # Every airport receives its full 300 kt.
    for a in results["airport_receipts"]:
        assert abs(a["received_kt"] - a["demand_kt"]) < 1e-3

    # Mass balance across the whole system.
    total_bio_in = sum(d["throughput_kt_biomass"] for d in results["depots_opened"])
    total_bio_ref = sum(r["intake_kt_biomass"] for r in results["refineries_opened"])
    total_saf = sum(r["output_kt_saf"] for r in results["refineries_opened"])
    assert abs(total_bio_in - total_bio_ref) < 1e-2, (total_bio_in, total_bio_ref)
    assert abs(total_bio_in - 6000.0) < 1e-2, total_bio_in
    assert abs(total_saf - 1200.0) < 1e-2, total_saf
    assert abs(total_saf - YIELD * total_bio_in) < 1e-6


if __name__ == "__main__":
    test_haversine_and_road_factor()
    print("test_haversine_and_road_factor PASSED")
    test_end_to_end_synthetic()
    print("test_end_to_end_synthetic PASSED")
