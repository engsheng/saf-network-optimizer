"""Unit tests for the SAF optimizer's building blocks.

Complements ``test_smoke.py`` (which solves the whole MILP end to end) with
fast, focused checks on the individual pieces: distance maths, the input
loaders, facility economics, and the structure of the built model. None of
these solve an optimization, so the whole file runs in well under a second.

Runnable two ways::

    pytest tests/test_units.py          # with pytest
    python tests/test_units.py          # plain, no pytest needed
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Make the package importable when running the file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saf import (
    CAPEX_YEARS,
    YIELD,
    build_model,
    load_airports,
    load_biomass,
    load_facilities,
    load_transport,
    synthetic_biomass,
)
from saf.distance import ROAD_FACTOR, haversine_km, road_km

DATA = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# Distance maths
# ---------------------------------------------------------------------------

def test_haversine_zero_distance():
    """A point to itself is exactly 0 km."""
    assert haversine_km(1.5, 110.0, 1.5, 110.0) == 0.0


def test_haversine_symmetric():
    """Distance A->B equals B->A."""
    a = haversine_km(-0.15071, 109.40400, -1.26827, 116.89400)
    b = haversine_km(-1.26827, 116.89400, -0.15071, 109.40400)
    assert math.isclose(a, b, rel_tol=1e-12)


def test_haversine_one_degree_latitude():
    """One degree of latitude is ~111 km anywhere on the globe."""
    d = haversine_km(0.0, 0.0, 1.0, 0.0)
    assert 110.0 < d < 112.0, d


def test_haversine_known_leg_pnk_bpn():
    """Pontianak (PNK) -> Balikpapan (BPN) great-circle is ~830 km."""
    d = haversine_km(-0.15071, 109.40400, -1.26827, 116.89400)
    assert 800.0 < d < 870.0, d


def test_road_factor_applied():
    """road_km is exactly ROAD_FACTOR times the great-circle distance."""
    gc = haversine_km(-0.15071, 109.40400, -1.26827, 116.89400)
    assert math.isclose(road_km(-0.15071, 109.40400, -1.26827, 116.89400),
                        ROAD_FACTOR * gc, rel_tol=1e-12)
    assert ROAD_FACTOR == 1.3


# ---------------------------------------------------------------------------
# Airport loader
# ---------------------------------------------------------------------------

def test_load_airports_count_and_demand():
    airports = load_airports(DATA / "airports.csv")
    assert len(airports) == 4
    assert sum(a.demand_kt for a in airports) == 1200.0
    assert all(a.demand_kt == 300.0 for a in airports)


def test_load_airports_trk_is_barge_only():
    """Tarakan is an island; its final leg must be flagged as a barge."""
    airports = {a.iata: a for a in load_airports(DATA / "airports.csv")}
    assert airports["TRK"].barge_only is True
    # No mainland airport should be flagged as barge.
    assert all(not a.barge_only for i, a in airports.items() if i != "TRK")


# ---------------------------------------------------------------------------
# Facility loader + economics
# ---------------------------------------------------------------------------

def test_load_facilities_shape():
    facilities = load_facilities(DATA / "parameters.xlsx")
    depots = [f for f in facilities if f.facility == "Depot"]
    refineries = [f for f in facilities if f.facility == "Refinery"]
    assert len(depots) == 2
    assert len(refineries) == 2
    assert {f.size for f in depots} == {"Small", "Large"}
    assert {f.size for f in refineries} == {"Small", "Large"}


def test_annualised_capex_is_straight_line():
    """Annualised CapEx = CapEx / 20 for every facility (zero interest)."""
    assert CAPEX_YEARS == 20
    for f in load_facilities(DATA / "parameters.xlsx"):
        assert math.isclose(f.annualised_capex_usd, f.capex_usd / 20.0,
                            rel_tol=1e-12)


def test_larger_facilities_have_more_capacity():
    """Within a facility type, Large must out-capacity Small."""
    facilities = load_facilities(DATA / "parameters.xlsx")
    for kind in ("Depot", "Refinery"):
        small = next(f for f in facilities
                     if f.facility == kind and f.size == "Small")
        large = next(f for f in facilities
                     if f.facility == kind and f.size == "Large")
        assert large.capacity_kt > small.capacity_kt
        assert large.capex_usd > small.capex_usd


# ---------------------------------------------------------------------------
# Transport loader
# ---------------------------------------------------------------------------

def test_load_transport_three_legs():
    transport = load_transport(DATA / "parameters.xlsx")
    assert set(transport) == {"FD", "DR", "RA"}


def test_transport_depot_leg_is_dearest():
    """The depot->refinery biomass rate is the highest per tonne-km, which is
    what makes moving unrefined biomass expensive (the core insight)."""
    transport = load_transport(DATA / "parameters.xlsx")
    assert transport["DR"].cost_usd_per_tkm > transport["FD"].cost_usd_per_tkm
    assert transport["DR"].cost_usd_per_tkm > transport["RA"].cost_usd_per_tkm


# ---------------------------------------------------------------------------
# Biomass loader (real organizer file) + synthetic fallback
# ---------------------------------------------------------------------------

def test_load_real_biomass_file():
    """The organizer's real CSV uses 'production_kilotons'/'location_id'
    column names; the loader must read them without error."""
    path = DATA / "biomass.csv"
    if not path.exists():
        return  # real file not shipped in this checkout; skip.
    farms = load_biomass(path)
    assert len(farms) == 80
    assert math.isclose(sum(f.biomass_kt for f in farms), 6800.0, rel_tol=1e-6)
    # IDs should come from the file, not the F000 fallback.
    assert all(not f.farm_id.startswith("F0") for f in farms[:5])


def test_synthetic_biomass_is_deterministic():
    """Same seed -> identical layout (reproducible CI)."""
    a = synthetic_biomass(n=80, total_kt=6800.0, seed=0)
    b = synthetic_biomass(n=80, total_kt=6800.0, seed=0)
    assert [f.biomass_kt for f in a] == [f.biomass_kt for f in b]
    assert [(f.lat, f.lon) for f in a] == [(f.lat, f.lon) for f in b]


def test_synthetic_biomass_total_rescaled():
    farms = synthetic_biomass(n=80, total_kt=6800.0, seed=7)
    assert len(farms) == 80
    assert math.isclose(sum(f.biomass_kt for f in farms), 6800.0, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Model structure (built, not solved)
# ---------------------------------------------------------------------------

def _build_small():
    airports = load_airports(DATA / "airports.csv")
    facilities = load_facilities(DATA / "parameters.xlsx")
    transport = load_transport(DATA / "parameters.xlsx")
    depot_specs = [f for f in facilities if f.facility == "Depot"]
    refinery_specs = [f for f in facilities if f.facility == "Refinery"]
    farms = synthetic_biomass(n=80, total_kt=6800.0, seed=0)
    candidates = [(f.farm_id, f.lat, f.lon) for f in farms]
    candidates.extend([(f"AIR_{a.iata}", a.lat, a.lon) for a in airports])
    built = build_model(
        farms=farms, airports=airports,
        depot_specs=depot_specs, refinery_specs=refinery_specs,
        transport=transport, candidate_sites=candidates,
    )
    return built, farms, airports


def test_model_candidate_count():
    """84 candidates = 80 biomass sites + 4 airports."""
    built, _, _ = _build_small()
    assert len(built["candidates"]) == 84
    assert sum(1 for c in built["candidates"] if c.startswith("AIR_")) == 4


def test_model_binary_variable_count():
    """336 binaries = 84 candidates x 2 sizes x {depot, refinery}."""
    built, _, _ = _build_small()
    n_binary = sum(1 for v in built["model"].variables()
                   if v.cat == "Integer")
    assert n_binary == 84 * 2 * 2, n_binary


def test_model_exposes_cost_components():
    """The objective is split into auditable named pieces."""
    built, _, _ = _build_small()
    expected = {
        "annualised_capex", "fixed_opex",
        "variable_opex_dep", "variable_opex_ref",
        "logistics_fd", "logistics_dr", "logistics_ra",
    }
    assert expected <= set(built["cost_components"])


def test_yield_constant():
    """Yield is the agreed 0.20 t SAF per t biomass."""
    assert YIELD == 0.20


# ---------------------------------------------------------------------------
# Plain-python runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
