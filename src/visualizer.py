"""
Map and coordinate table for the simplified dashboard.

- Base map: Carto (free, no API token required). Shows Kalimantan's coastline,
  country borders and major place names underneath the markers.
- Tooltips: plain text with newlines (no HTML), so no stray `<b>` tags.
"""

from typing import Dict, Any
import pandas as pd
import pydeck as pdk


# Palette (consistent across markers and arcs)
COLOUR = {
    "biomass_used":   [34, 139, 34, 180],    # green
    "biomass_unused": [120, 120, 120, 60],   # faded grey
    "depot":          [255, 140, 0, 230],    # orange
    "refinery":       [220, 20, 60, 240],    # crimson
    "airport":        [30, 144, 255, 240],   # blue
    "arc_biomass":    [34, 139, 34],         # green
    "arc_pre":        [255, 140, 0],         # orange
    "arc_saf":        [220, 20, 60],         # red
    "arc_barge":      [138, 43, 226],        # purple
}

VIEW = pdk.ViewState(latitude=0.0, longitude=114.0, zoom=5.3, pitch=35)


def _facility_rows(solution: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for f in solution["facilities"]:
        rows.append({
            "lat": f["lat"], "lon": f["lon"],
            "colour": COLOUR[f["type"]],
            "radius": 15000 if f["size"] == "Large" else 9000,
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
            "radius": 20000,
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
    """One row per flow. Includes a tooltip with the material and tonnage."""
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
        if f["tonnes"] < 1:            # hide noise
            continue
        if f["distance_km"] < 0.5:     # hide co-located nodes
            continue
        src = coords(f["from_id"])
        dst = coords(f["to_id"])
        if src is None or dst is None:
            continue
        if f["mode"] == "barge":
            colour = COLOUR["arc_barge"]
            material = "SAF (barge)"
        elif f["stage"] == "farm_to_depot":
            colour = COLOUR["arc_biomass"]
            material = "biomass"
        elif f["stage"] == "depot_to_refinery":
            colour = COLOUR["arc_pre"]
            material = "biomass"
        else:
            colour = COLOUR["arc_saf"]
            material = "SAF"
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


def build_map(solution: Dict[str, Any],
              biomass: pd.DataFrame,
              airports: pd.DataFrame) -> pdk.Deck:
    """pydeck Deck with Carto base map (no API token required)."""
    layers = [
        pdk.Layer(
            "ScatterplotLayer", data=_biomass_rows(biomass, solution),
            get_position="[longitude, latitude]",
            get_fill_color="colour", get_radius="radius",
            pickable=True, opacity=0.7,
        ),
        pdk.Layer(
            "ArcLayer", data=_arc_rows(solution, biomass, airports),
            get_source_position="[from_lon, from_lat]",
            get_target_position="[to_lon, to_lat]",
            get_source_color="colour", get_target_color="colour",
            get_width="width", pickable=True,
        ),
        pdk.Layer(
            "ScatterplotLayer", data=_facility_rows(solution),
            get_position="[lon, lat]",
            get_fill_color="colour", get_radius="radius",
            pickable=True, stroked=True,
            get_line_color=[255, 255, 255], line_width_min_pixels=2,
        ),
        pdk.Layer(
            "ScatterplotLayer", data=_airport_rows(airports, solution),
            get_position="[lon, lat]",
            get_fill_color="colour", get_radius="radius",
            pickable=True, stroked=True,
            get_line_color=[255, 255, 255], line_width_min_pixels=2,
        ),
    ]
    # Plain-text tooltip. white-space: pre-line respects the \n in the text,
    # so the three-line format from `tooltip` fields renders as separate lines.
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
    # Carto's "dark_matter" / "positron" tilesets are free (no API token).
    return pdk.Deck(
        layers=layers,
        initial_view_state=VIEW,
        map_provider="carto",
        map_style="dark",
        tooltip=tooltip,
    )


# ---------------------------------------------------------------------------
# Simple tables
# ---------------------------------------------------------------------------

def facility_counts(solution: Dict[str, Any]) -> dict:
    """{('depot','Large'): 15, …}"""
    counts = {}
    for f in solution["facilities"]:
        counts[(f["type"], f["size"])] = counts.get((f["type"], f["size"]), 0) + 1
    return counts


def coordinates_table(solution: Dict[str, Any]) -> pd.DataFrame:
    """Refineries first, then depots by throughput descending."""
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
