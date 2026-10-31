"""
Load fixed problem data: 4 airports and 80 biomass sites.

Biomass loading now delegates to the saf package so the site IDs that the
dashboard renders match the IDs the solver references in its flows.
"""

from pathlib import Path
import pandas as pd

from saf.data import load_biomass as _load_biomass_saf, synthetic_biomass

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


# ---------------------------------------------------------------------------
# Airports
# ---------------------------------------------------------------------------

def load_airports() -> pd.DataFrame:
    """DataFrame of airports. Checks data/airports.csv then the long name."""
    for name in ("airports.csv", "challenge3_airports_4.csv"):
        p = DATA_DIR / name
        if p.exists():
            return pd.read_csv(p)
    raise FileNotFoundError(
        "No airports CSV found. Expected data/airports.csv or "
        "data/challenge3_airports_4.csv."
    )


# ---------------------------------------------------------------------------
# Biomass — delegates to the saf package so site IDs match the solver
# ---------------------------------------------------------------------------

def load_biomass() -> pd.DataFrame:
    biomass_csv = DATA_DIR / "biomass.csv"
    if biomass_csv.exists():
        farms = _load_biomass_saf(biomass_csv)
    else:
        farms = synthetic_biomass(n=80, total_kt=6800.0)
    return pd.DataFrame([
        {
            "site_id": f.farm_id,
            "latitude": f.lat,
            "longitude": f.lon,
            "capacity_kt_per_year": f.biomass_kt,
        }
        for f in farms
    ])
