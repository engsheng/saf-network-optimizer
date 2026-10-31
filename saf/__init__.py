"""SAF supply-chain network optimizer.

Minimises annualised CapEx + fixed OpEx + variable OpEx + logistics across a
three-echelon network (farms -> depots -> refineries -> airports) subject to
yield, capacity and demand constraints.

See the Design Approach document for the core economic insight and expected
solution structure, and the Modeling Assumptions document for parameter choices.
"""

from .data import (
    YIELD, CAPEX_YEARS,
    Airport, Farm, FacilitySpec, TransportLeg,
    load_airports, load_facilities, load_transport,
    load_biomass, synthetic_biomass,
)
from .distance import haversine_km, road_km, ROAD_FACTOR
from .model import build_model
from .validate import validate_solution, insight_warnings
from .report import print_summary, extract_results

__all__ = [
    "YIELD", "CAPEX_YEARS", "ROAD_FACTOR",
    "Airport", "Farm", "FacilitySpec", "TransportLeg",
    "load_airports", "load_facilities", "load_transport",
    "load_biomass", "synthetic_biomass",
    "haversine_km", "road_km",
    "build_model", "validate_solution", "insight_warnings",
    "print_summary", "extract_results",
]
