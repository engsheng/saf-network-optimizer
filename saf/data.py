"""Loaders for the Airbus Challenge #3 input data.

Sources:
  - airports CSV (challenge3_airports_4.csv): 4 rows, IATA + coords + demand
  - parameters XLSX (challenge3_parameters.xlsx): facility costs/capacity,
    transport rates, assumptions sheet
  - biomass CSV (kalimantan_80_locations_6800kt_full_range.csv): 80 sites
    with lat/lon and biomass availability

A synthetic 80-farm fallback is provided for smoke tests when the biomass
file is not on disk. It must NOT be used for final evaluation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Union

import pandas as pd


# Yield η: 1 t biomass -> 0.20 t SAF (Challenge #3 clarification + parameters sheet).
YIELD: float = 0.20

# CapEx amortisation (straight-line, zero interest, per clarifications).
CAPEX_YEARS: int = 20


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    lat: float
    lon: float
    demand_kt: float
    # Juwata (TRK) is on an island; the final leg is modelled as a barge at
    # the same 0.08 USD/t-km rate (clarification). The optimizer treats the
    # arithmetic identically but we flag the route for output traceability.
    barge_only: bool = False


@dataclass(frozen=True)
class Farm:
    farm_id: str
    lat: float
    lon: float
    biomass_kt: float  # kt of biomass available per year at this site


@dataclass(frozen=True)
class FacilitySpec:
    """One row of the facilities parameters sheet."""
    facility: str                          # 'Depot' | 'Refinery'
    size: str                              # 'Small' | 'Large'
    capex_usd: float
    capacity_kt: float                     # biomass intake capacity (kt/yr)
    fixed_opex_usd: float                  # per year
    variable_opex_usd_per_t: float         # per tonne of biomass processed

    @property
    def annualised_capex_usd(self) -> float:
        """CapEx straight-line over 20 years, no interest (per clarifications)."""
        return self.capex_usd / CAPEX_YEARS


@dataclass(frozen=True)
class TransportLeg:
    leg: str                               # 'FD' | 'DR' | 'RA'
    cost_usd_per_tkm: float                # USD per tonne-km (same for both truck sizes)
    small_truck_t: float
    large_truck_t: float


# -----------------------------------------------------------------------------
# Loaders
# -----------------------------------------------------------------------------

PathLike = Union[str, Path]


def load_airports(path: PathLike) -> List[Airport]:
    """Load the 4-airport CSV. TRK is flagged barge_only."""
    df = pd.read_csv(path)
    out: List[Airport] = []
    for _, r in df.iterrows():
        iata = str(r["iata"]).strip()
        out.append(Airport(
            iata=iata,
            name=str(r["name"]),
            city=str(r["city"]),
            lat=float(r["latitude"]),
            lon=float(r["longitude"]),
            demand_kt=float(r["saf_demand_kt_per_year"]),
            barge_only=(iata == "TRK"),
        ))
    return out


def load_facilities(xlsx_path: PathLike) -> List[FacilitySpec]:
    """Load the 4-row facilities sheet (Depot Small/Large, Refinery Small/Large).

    The organizer's workbook has a one-row descriptive note and a blank
    row before the real column header, so we start at row 2 (0-indexed).
    """
    df = pd.read_excel(xlsx_path, sheet_name="facilities", header=2)
    out: List[FacilitySpec] = []
    for _, r in df.iterrows():
        out.append(FacilitySpec(
            facility=str(r["facility"]).strip(),
            size=str(r["size"]).strip(),
            capex_usd=float(r["capex_usd"]),
            capacity_kt=float(r["capacity_kt_biomass_in_per_year"]),
            fixed_opex_usd=float(r["fixed_opex_usd_per_year"]),
            variable_opex_usd_per_t=float(r["variable_opex_usd_per_tonne_in"]),
        ))
    return out


def load_transport(xlsx_path: PathLike) -> Dict[str, TransportLeg]:
    """Load the transport sheet. Returns {'FD', 'DR', 'RA'}.

    Same header offset as load_facilities (note row + blank row).
    """
    df = pd.read_excel(xlsx_path, sheet_name="transport", header=2)
    name_map = {
        "farm to depot": "FD",
        "depot to refinery": "DR",
        "refinery to airport": "RA",
    }
    legs: Dict[str, TransportLeg] = {}
    for _, r in df.iterrows():
        key = name_map.get(str(r["leg"]).strip().lower())
        if key is None:
            continue
        legs[key] = TransportLeg(
            leg=key,
            cost_usd_per_tkm=float(r["cost_usd_per_tonne_km"]),
            small_truck_t=float(r["small_truck_t"]),
            large_truck_t=float(r["large_truck_t"]),
        )
    missing = {"FD", "DR", "RA"} - legs.keys()
    if missing:
        raise ValueError(f"transport sheet missing legs: {missing}")
    return legs


def load_biomass(path: PathLike) -> List[Farm]:
    """Load the biomass locations CSV.

    Column names vary slightly between organizer file revisions, so we probe
    for common synonyms before giving up.
    """
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}

    def pick(options: List[str], required: bool = True) -> str:
        for opt in options:
            if opt in cols:
                return cols[opt]
        if required:
            raise KeyError(f"none of {options} found in {list(df.columns)}")
        return ""

    lat_c = pick(["latitude", "lat", "y"])
    lon_c = pick(["longitude", "lon", "lng", "long", "x"])
    kt_c = pick([
        "biomass_kt_per_year", "biomass_kt_year", "biomass_kt",
        "biomass", "kt_per_year", "kt",
    ])
    id_c = pick(["id", "farm_id", "site_id", "name"], required=False)

    farms: List[Farm] = []
    for i, r in df.iterrows():
        farm_id = str(r[id_c]) if id_c else f"F{i:03d}"
        farms.append(Farm(
            farm_id=farm_id,
            lat=float(r[lat_c]),
            lon=float(r[lon_c]),
            biomass_kt=float(r[kt_c]),
        ))
    return farms


def synthetic_biomass(n: int = 80, total_kt: float = 6800.0, seed: int = 42) -> List[Farm]:
    """Synthetic 80-farm layout over Kalimantan land bounds.

    Intended ONLY for smoke tests / CI. Site sizes are drawn uniformly from
    20-100 kt (matching the organizer's description) and rescaled so the
    total equals ``total_kt``. Positions are uniform over the rough bounding
    box of Kalimantan land (lat -4.0..4.5, lon 109.5..118.0) -- some may
    land in the sea; the organizer's real file avoids this.
    """
    rng = random.Random(seed)
    raw_sizes = [rng.uniform(20.0, 100.0) for _ in range(n)]
    scale = total_kt / sum(raw_sizes)
    sizes = [s * scale for s in raw_sizes]
    farms: List[Farm] = []
    for i in range(n):
        farms.append(Farm(
            farm_id=f"F{i:03d}",
            lat=rng.uniform(-4.0, 4.5),
            lon=rng.uniform(109.5, 118.0),
            biomass_kt=sizes[i],
        ))
    return farms
