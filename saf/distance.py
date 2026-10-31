"""Great-circle distance with the organizer's 1.3 road factor.

We deliberately do NOT include altitude (see the Modeling Assumptions doc,
section "Why we exclude altitude"): the 1.3 multiplier already absorbs
terrain, altitude corrections are negligible at Kalimantan's scale, and
trucks follow roads rather than hypotenuses.

The same formula is applied to Tarakan's barge leg per the organizer's
Challenge #3 clarification (same per-tonne-km rate, no extra fixed cost).
"""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0
ROAD_FACTOR = 1.3


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points on Earth in kilometres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def road_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance scaled by the ROAD_FACTOR (1.3)."""
    return ROAD_FACTOR * haversine_km(lat1, lon1, lat2, lon2)
