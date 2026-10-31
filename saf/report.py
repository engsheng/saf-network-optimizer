"""Pretty-print the solved solution and extract it as plain Python dicts."""

from __future__ import annotations

from typing import Any, Dict, List

import pulp

from .data import YIELD, Airport, Farm


def _val(v) -> float:
    val = pulp.value(v)
    return 0.0 if val is None else float(val)


def extract_results(
    built: Dict[str, Any],
    farms: List[Farm],
    airports: List[Airport],
    flow_tol: float = 1e-4,
) -> Dict[str, Any]:
    """Return a plain-dict snapshot of the solution for JSON serialisation."""
    model = built["model"]
    CANDS: List[str] = built["candidates"]
    cand_of = built["cand_of"]
    DEP_SIZES: List[str] = built["sizes"]["dep"]
    REF_SIZES: List[str] = built["sizes"]["ref"]
    y_dep, y_ref = built["y_dep"], built["y_ref"]
    x, w, z = built["x"], built["w"], built["z"]
    d_fd = built["distances"]["fd"]
    d_dr = built["distances"]["dr"]
    d_ra = built["distances"]["ra"]

    depots = []
    for c in CANDS:
        for s in DEP_SIZES:
            if _val(y_dep[c][s]) > 0.5:
                thru = sum(_val(x[f.farm_id][c][s]) for f in farms)
                lat, lon = cand_of[c]
                depots.append({
                    "site_id": c, "size": s,
                    "lat": lat, "lon": lon,
                    "throughput_kt_biomass": thru,
                })

    refineries = []
    for c in CANDS:
        for sr in REF_SIZES:
            if _val(y_ref[c][sr]) > 0.5:
                in_bio = sum(_val(w[d][c][sr]) for d in CANDS)
                lat, lon = cand_of[c]
                refineries.append({
                    "site_id": c, "size": sr,
                    "lat": lat, "lon": lon,
                    "intake_kt_biomass": in_bio,
                    "output_kt_saf": YIELD * in_bio,
                })

    airport_recv = []
    for a in airports:
        got = sum(_val(z[r][a.iata][sr]) for r in CANDS for sr in REF_SIZES)
        airport_recv.append({
            "iata": a.iata, "demand_kt": a.demand_kt, "received_kt": got,
            "barge_only": a.barge_only,
        })

    flows_fd = []
    for f in farms:
        for c in CANDS:
            for s in DEP_SIZES:
                v = _val(x[f.farm_id][c][s])
                if v > flow_tol:
                    flows_fd.append({
                        "farm": f.farm_id, "depot": c, "depot_size": s,
                        "flow_kt": v, "distance_km": d_fd[(f.farm_id, c)],
                    })
    flows_dr = []
    for d in CANDS:
        for r in CANDS:
            for sr in REF_SIZES:
                v = _val(w[d][r][sr])
                if v > flow_tol:
                    flows_dr.append({
                        "depot": d, "refinery": r, "refinery_size": sr,
                        "flow_kt": v, "distance_km": d_dr[(d, r)],
                    })
    flows_ra = []
    for r in CANDS:
        for a in airports:
            for sr in REF_SIZES:
                v = _val(z[r][a.iata][sr])
                if v > flow_tol:
                    flows_ra.append({
                        "refinery": r, "airport": a.iata, "refinery_size": sr,
                        "flow_kt": v, "distance_km": d_ra[(r, a.iata)],
                        "mode": "barge" if a.barge_only else "truck",
                    })

    cc = built["cost_components"]
    cost_breakdown = {name: _val(expr) for name, expr in cc.items()}
    total = sum(cost_breakdown.values())

    return {
        "total_annual_cost_usd": total,
        "cost_breakdown_usd": cost_breakdown,
        "depots_opened": depots,
        "refineries_opened": refineries,
        "airport_receipts": airport_recv,
        "flows_farm_to_depot": flows_fd,
        "flows_depot_to_refinery": flows_dr,
        "flows_refinery_to_airport": flows_ra,
    }


def print_summary(
    built: Dict[str, Any],
    farms: List[Farm],
    airports: List[Airport],
) -> None:
    """Human-readable summary to stdout."""
    results = extract_results(built, farms, airports)
    total = results["total_annual_cost_usd"]

    print()
    print("=" * 68)
    print("  SAF SUPPLY CHAIN -- OPTIMAL SOLUTION")
    print("=" * 68)
    print(f"  Total annual cost: {total:>18,.0f} USD/yr   "
          f"({total/1e6:,.2f} M USD/yr)")
    print()
    print("  Cost breakdown (USD/yr):")
    for name, val in results["cost_breakdown_usd"].items():
        pct = 100.0 * val / total if total else 0.0
        print(f"    {name:22s} {val:>18,.0f}  ({pct:5.1f}%)")

    print()
    print(f"  Facilities opened:  {len(results['depots_opened'])} depots, "
          f"{len(results['refineries_opened'])} refineries")
    print()
    print("  Depots opened:")
    print(f"    {'site':>14s}  {'size':>5s}  {'lat':>8s}  {'lon':>9s}  "
          f"{'throughput_kt':>14s}")
    for d in results["depots_opened"]:
        print(f"    {d['site_id']:>14s}  {d['size']:>5s}  "
              f"{d['lat']:>8.4f}  {d['lon']:>9.4f}  {d['throughput_kt_biomass']:>14.1f}")

    print()
    print("  Refineries opened:")
    print(f"    {'site':>14s}  {'size':>5s}  {'lat':>8s}  {'lon':>9s}  "
          f"{'intake_kt':>12s}  {'saf_out_kt':>12s}")
    for r in results["refineries_opened"]:
        print(f"    {r['site_id']:>14s}  {r['size']:>5s}  "
              f"{r['lat']:>8.4f}  {r['lon']:>9.4f}  "
              f"{r['intake_kt_biomass']:>12.1f}  {r['output_kt_saf']:>12.1f}")

    print()
    print("  Airport receipts:")
    for a in results["airport_receipts"]:
        tag = "(barge)" if a["barge_only"] else ""
        delivered = a["received_kt"]
        tick = "OK" if abs(delivered - a["demand_kt"]) < 1e-3 else "MISMATCH"
        print(f"    {a['iata']}  demand={a['demand_kt']:.0f} kt  "
              f"received={delivered:7.2f} kt  {tick}  {tag}")

    # Flow summaries (counts only; full flows are in the JSON output).
    print()
    print(f"  Flows active: FD={len(results['flows_farm_to_depot'])}  "
          f"DR={len(results['flows_depot_to_refinery'])}  "
          f"RA={len(results['flows_refinery_to_airport'])}")
    print("=" * 68)
