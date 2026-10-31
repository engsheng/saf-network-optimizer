"""
Map and coordinate table for the simplified dashboard.

- Base map: Carto (free, no API token).
- Tooltips: plain text with newlines rendered via CSS white-space: pre-line.
- Facility markers: split into separate layers so Large/Small have distinct
  colours, radii and pixel caps. Pixel caps prevent markers from engulfing
  the map when zoomed in.
"""

from typing import Dict, Any, List
import pandas as pd
import pydeck as pdk


# Colour palette.
# Hue encodes TYPE (red = refinery, orange = depot, blue = airport, green = biomass).
# Saturation encodes SIZE (strong = Large, pale = Small).
COLOUR = {
    "refinery_large": [178, 34, 52,  240],   # firebrick
    "refinery_small": [255, 120, 140, 220],  # salmon
    "depot_large":    [255, 140, 0,   235],  # dark orange
    "depot_small":    [255, 195, 110, 215],  # light orange
    "airport":        [30, 144, 255,  240],  # dodger blue
    "biomass_used":   [34, 139, 34,   180],  # forest green
    "biomass_unused": [120, 120, 120, 60],   # faded grey
    # Arc colours
    "arc_biomass":    [34, 139, 34],
    "arc_pre":        [255, 140, 0],
    "arc_saf":        [220, 20, 60],
    "arc_barge":      [138, 43, 226],
}

# Marker sizing. radius_m is a nominal size in metres; max_px is the hard cap
# in screen pixels so the marker never grows larger than this when zoomed in.
MARKER = {
    "refinery_large": {"radius_m": 18000, "max_px": 60, "stroke_px": 3},
    "refinery_small": {"radius_m": 10000, "max_px": 35, "stroke_px": 2},
    "depot_large":    {"radius_m": 11000, "max_px": 36, "stroke_px": 2},
    "depot_small":    {"radius_m":  6000, "max_px": 20, "stroke_px": 1},
    "airport":        {"radius_m": 15000, "max_px": 44, "stroke_px": 3},
    "biomass":        {"max_px":   16, "min_px": 3},
}

VIEW = pdk.ViewState(latitude=0.0, longitude=114.0, zoom=5.3, pitch=35)


# ---------------------------------------------------------------------------
# Helpers to build per-category DataFrames
# ---------------------------------------------------------------------------

def _facility_rows(solution: Dict[str, Any],
                   facility_type: str,
                   size: str) -> pd.DataFrame:
    """Rows for one (type, size) combo. Empty frame if none built."""
    key = f"{facility_type}_{size.lower()}"
    rows = []
    for f in solution["facilities"]:
        if f["type"] != facility_type or f["size"] != size:
            continue
        rows.append({
            "lat": f["lat"], "lon": f["lon"],
            "colour": COLOUR[key],
            "radius": MARKER[key]["radius_m"],
            "tooltip": (
                f"{f['id']}\n"
                f"{f['type'].title()} ({f['size']})\n"
                f"Throughput: {f['throughput_kt_in']:.1f} / "
                f"{f['capacity_kt_in']:.0f} kt·yr\n"
                f"Lat {f['lat']:.4f}, Lon {f['lon']:.4f}"
            ),
        })
    return pd.DataFrame(rows)


def _biomass_rows(biomass: pd.DataFrame, solution: Dict[str, Any]) -> pd.DataFrame:
    used = {f["from_id"] for f in solution["flows"]
            if f["stage"] == "farm_to_depot" and f["tonnes"] > 1}
    df = biomass.copy()
    df["used"] = df["site_id"].isin(used)
    df["colour"] = df["used"].apply(
        lambda u: COLOUR["biomass_used"] if u else COLOUR["biomass_unused"]
    )
    df["radius"] = df["capacity_kt_per_year"] * 60
    df["tooltip"] = df.apply(
        lambda r: (
            f"{r.site_id} (biomass site)\n"
            f"Available: {r.capacity_kt_per_year:.1f} kt/yr\n"
            f"{'In use' if r.used else 'Not used'}"
        ), axis=1,
    )
    return df


def _airport_rows(airports: pd.DataFrame, solution: Dict[str, Any]) -> pd.DataFrame:
    delivery = {a["iata"]: a for a in solution["airport_delivery"]}
    rows = []
    for _, a in airports.iterrows():
        d = delivery.get(a.iata, {"demand_kt": a.saf_demand_kt_per_year,
                                   "delivered_kt": 0})
        met = d["delivered_kt"] + 1e-3 >= d["demand_kt"]
        rows.append({
            "lat": a.latitude, "lon": a.longitude,
            "colour": COLOUR["airport"],
            "radius": MARKER["airport"]["radius_m"],
            "tooltip": (
                f"{a.iata} — {a['name']}\n"
                f"SAF: {d['delivered_kt']:.1f} / {d['demand_kt']:.0f} kt·yr "
                f"{'✓' if met else '✗ short'}"
            ),
        })
    return pd.DataFrame(rows)


def _arc_rows(solution: Dict[str, Any],
              biomass: pd.DataFrame,
              airports: pd.DataFrame) -> pd.DataFrame:
    bio = biomass.set_index("site_id")[["latitude", "longitude"]].to_dict("index")
    air = airports.set_index("iata")[["latitude", "longitude"]].to_dict("index")
    fac = {f["id"]: (f["lat"], f["lon"]) for f in solution["facilities"]}

    def coords(node_id):
        if node_id in fac:
            return fac[node_id]
        if node_id in bio:
            return bio[node_id]["latitude"], bio[node_id]["longitude"]
        if node_id in air:
            return air[node_id]["latitude"], air[node_id]["longitude"]
        return None

    rows = []
    for f in solution["flows"]:
        if f["tonnes"] < 1 or f["distance_km"] < 0.5:
            continue
        src = coords(f["from_id"])
        dst = coords(f["to_id"])
        if src is None or dst is None:
            continue
        if f["mode"] == "barge":
            colour, material = COLOUR["arc_barge"], "SAF (barge)"
        elif f["stage"] == "farm_to_depot":
            colour, material = COLOUR["arc_biomass"], "biomass"
        elif f["stage"] == "depot_to_refinery":
            colour, material = COLOUR["arc_pre"], "biomass"
        else:
            colour, material = COLOUR["arc_saf"], "SAF"
        kt = f["tonnes"] / 1000.0
        rows.append({
            "from_lat": src[0], "from_lon": src[1],
            "to_lat": dst[0],   "to_lon": dst[1],
            "colour": colour,
            "width": min(8.0, max(0.6, kt / 50.0)),
            "tooltip": (
                f"{kt:,.1f} kt·yr {material}\n"
                f"{f['from_id']} → {f['to_id']}\n"
                f"{f['distance_km']:.0f} km"
            ),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Deck
# ---------------------------------------------------------------------------

def _scatter(data: pd.DataFrame, max_px: int, min_px: int = 2,
             stroke_px: int = 0) -> pdk.Layer:
    """Shared ScatterplotLayer builder with pixel caps for zoom-safety."""
    kwargs = dict(
        type="ScatterplotLayer", data=data,
        get_position="[lon, lat]",
        get_fill_color="colour", get_radius="radius",
        radius_min_pixels=min_px,
        radius_max_pixels=max_px,
        pickable=True,
    )
    if stroke_px:
        kwargs.update(
            stroked=True,
            get_line_color=[255, 255, 255],
            line_width_min_pixels=stroke_px,
        )
    return pdk.Layer(**kwargs)


def build_map(solution: Dict[str, Any],
              biomass: pd.DataFrame,
              airports: pd.DataFrame) -> pdk.Deck:
    layers: List[pdk.Layer] = []

    # Biomass behind everything else, with a smaller pixel cap
    bio_df = _biomass_rows(biomass, solution)
    bio_df = bio_df.rename(columns={"latitude": "lat", "longitude": "lon"})
    layers.append(_scatter(
        bio_df,
        max_px=MARKER["biomass"]["max_px"],
        min_px=MARKER["biomass"]["min_px"],
    ))

    # Arcs between biomass and facility layers so markers sit on top
    arc_df = _arc_rows(solution, biomass, airports)
    layers.append(pdk.Layer(
        "ArcLayer", data=arc_df,
        get_source_position="[from_lon, from_lat]",
        get_target_position="[to_lon, to_lat]",
        get_source_color="colour", get_target_color="colour",
        get_width="width", pickable=True,
    ))

    # Four facility layers so each (type, size) has its own radius/colour/cap.
    # Order: Small on bottom, Large on top (so Large doesn't get hidden).
    for ftype, fsize in (("depot", "Small"), ("depot", "Large"),
                         ("refinery", "Small"), ("refinery", "Large")):
        df = _facility_rows(solution, ftype, fsize)
        if df.empty:
            continue
        key = f"{ftype}_{fsize.lower()}"
        layers.append(_scatter(
            df,
            max_px=MARKER[key]["max_px"],
            stroke_px=MARKER[key]["stroke_px"],
        ))

    # Airports on top of everything
    layers.append(_scatter(
        _airport_rows(airports, solution),
        max_px=MARKER["airport"]["max_px"],
        stroke_px=MARKER["airport"]["stroke_px"],
    ))

    tooltip = {
        "text": "{tooltip}",
        "style": {
            "backgroundColor": "#1e1e1e",
            "color": "white",
            "fontSize": "13px",
            "padding": "8px 12px",
            "borderRadius": "6px",
            "whiteSpace": "pre-line",
        },
    }
    return pdk.Deck(
        layers=layers,
        initial_view_state=VIEW,
        map_provider="carto",
        map_style="dark",
        tooltip=tooltip,
    )


# ---------------------------------------------------------------------------
# Tables for the simplified dashboard
# ---------------------------------------------------------------------------

def facility_counts(solution: Dict[str, Any]) -> dict:
    counts = {}
    for f in solution["facilities"]:
        counts[(f["type"], f["size"])] = counts.get((f["type"], f["size"]), 0) + 1
    return counts


def coordinates_table(solution: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for f in solution["facilities"]:
        rows.append({
            "ID": f["id"],
            "Type": f["type"].title(),
            "Size": f["size"],
            "Latitude": round(f["lat"], 4),
            "Longitude": round(f["lon"], 4),
            "Throughput (kt/yr)": round(f["throughput_kt_in"], 1),
        })
    df = pd.DataFrame(rows)
    df["_order"] = df.apply(
        lambda r: (0 if r.Type == "Refinery" else 1, -r["Throughput (kt/yr)"]),
        axis=1,
    )
    df = df.sort_values("_order").drop(columns="_order").reset_index(drop=True)
    return df
