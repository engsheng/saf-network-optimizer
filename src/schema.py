"""
Solution contract between the optimiser and the dashboard.

Keep this file stable. The MILP team writes to this schema; the viz team
reads it. If a field needs to change, update this doc first.

Everything is plain dicts/lists so it serialises to JSON cleanly.
"""

from typing import TypedDict, List, Literal, Optional


class CostBreakdown(TypedDict):
    capex_annualised_usd: float   # straight-line / 20 yrs, no interest
    fixed_opex_usd: float          # 5% of CapEx per year
    variable_opex_usd: float       # USD/t_in * tonnes through each facility
    trucking_usd: float            # tonne-km * truck rates (farm->depot, depot->refinery)
    barge_usd: float               # TRK leg only, 0.08 USD/t-km
    total_usd: float               # == sum of the above


class Facility(TypedDict):
    id: str                        # "D1", "R1", etc. Stable across re-solves.
    type: Literal["depot", "refinery"]
    size: Literal["Small", "Large"]
    lat: float
    lon: float
    capacity_kt_in: float          # biomass intake capacity (kt/yr)
    throughput_kt_in: float        # actual biomass tonnes/yr through this facility
    utilisation: float             # throughput / capacity, in [0, 1]
    annualised_capex_usd: float
    fixed_opex_usd: float
    variable_opex_usd: float


class Flow(TypedDict):
    from_id: str                   # biomass site id, depot id, or refinery id
    to_id: str                     # depot, refinery, or airport IATA
    stage: Literal["farm_to_depot", "depot_to_refinery", "refinery_to_airport"]
    tonnes: float                  # for refinery->airport, this is SAF tonnes
    distance_km: float             # great-circle * 1.3 (barge for TRK)
    cost_usd: float
    mode: Literal["truck", "barge"]


class AirportDelivery(TypedDict):
    iata: str
    demand_kt: float
    delivered_kt: float
    sources: List[str]             # refinery ids feeding this airport


class Solution(TypedDict):
    status: Literal["optimal", "infeasible", "unbounded", "error"]
    solve_time_sec: float
    # "proven_optimal" | "time_limited_incumbent" | None (not recorded)
    optimality_status: Optional[str]
    optimality_gap: Optional[float]  # relative MIP gap; None if unknown
    objective_value_usd: float     # == cost_breakdown.total_usd
    cost_breakdown: CostBreakdown
    facilities: List[Facility]
    flows: List[Flow]
    airport_delivery: List[AirportDelivery]
    notes: Optional[str]           # free-text, surfaced in the UI as a caveat


# ---------- helpers for the viz team ----------

def validate_solution(sol: dict) -> List[str]:
    """
    Light checks surfaced in the UI. The judges can see these in a 'diagnostics'
    expander, which hits the Trust & Traceability criterion.
    """
    errors = []
    if sol.get("status") != "optimal":
        errors.append(f"Solver status: {sol.get('status')}")
    # Demand met?
    for a in sol.get("airport_delivery", []):
        if a["delivered_kt"] + 1e-6 < a["demand_kt"]:
            errors.append(
                f"Airport {a['iata']} underserved: {a['delivered_kt']:.1f} < {a['demand_kt']:.1f} kt"
            )
    # Cost identity
    cb = sol.get("cost_breakdown", {})
    pieces = ["capex_annualised_usd", "fixed_opex_usd", "variable_opex_usd",
              "trucking_usd", "barge_usd"]
    if cb:
        total_check = sum(cb.get(k, 0) for k in pieces)
        if abs(total_check - cb.get("total_usd", 0)) > 1.0:
            errors.append(
                f"Cost breakdown doesn't sum to total: {total_check:.0f} vs {cb['total_usd']:.0f}"
            )
    return errors
