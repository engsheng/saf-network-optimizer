"""
Build the map and charts for the dashboard.

- build_map(): pydeck Deck object with biomass, facilities, airports, flow arcs.
- cost_breakdown_chart(): plotly stacked bar (vs baseline).
- facility_table(), flow_table(): pandas DataFrames ready for st.dataframe.
"""

from typing import Dict, Any, List
import pandas as pd
import pydeck as pdk
import plotly.graph_objects as go

# ---------- colour palette (consistent across map and charts) ----------
COLOUR = {
    "biomass_used":    [34, 139, 34, 180],    # forest green
    "biomass_unused":  [120, 120, 120, 60],   # grey, low alpha
    "depot":           [255, 140, 0, 220],    # orange
    "refinery":        [220, 20, 60, 230],    # crimson
    "airport":         [30, 144, 255, 240],   # dodger blue
    "arc_biomass":     [34, 139, 34],
    "arc_intermediate":[255, 140, 0],
    "arc_saf":         [220, 20, 60],
    "arc_barge":       [138, 43, 226],        # purple, visually distinct
}

VIEW = pdk.ViewState(latitude=0.0, longitude=114.0, zoom=5.3, pitch=35)


def _facility_marker_df(solution: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for f in solution["facilities"]:
        rows.append({
            "id": f["id"],
            "label": f'{f["id"]} ({f["type"][:1].upper()}, {f["size"][:1]})',
            "type": f["type"],
            "size": f["size"],
            "lat": f["lat"],
            "lon": f["lon"],
            "utilisation": f["utilisation"],
            "throughput_kt": round(f["throughput_kt_in"], 1),
            "colour": COLOUR[f["type"]],
            # Pydeck ScatterplotLayer radius is in metres; scale by size.
            "radius": 15000 if f["size"] == "Large" else 9000,
        })
    return pd.DataFrame(rows)


def _biomass_df(biomass: pd.DataFrame, solution: Dict[str, Any]) -> pd.DataFrame:
    used_ids = {
        f["from_id"] for f in solution["flows"]
        if f["stage"] == "farm_to_depot" and f["tonnes"] > 1e-3
    }
    df = biomass.copy()
    df["used"] = df["site_id"].isin(used_ids)
    df["colour"] = df["used"].apply(
        lambda u: COLOUR["biomass_used"] if u else COLOUR["biomass_unused"]
    )
    df["radius"] = df["capacity_kt_per_year"] * 60  # ~1.2-6 km
    return df


def _airport_df(airports: pd.DataFrame, solution: Dict[str, Any]) -> pd.DataFrame:
    delivery = {a["iata"]: a for a in solution["airport_delivery"]}
    rows = []
    for _, a in airports.iterrows():
        d = delivery.get(a.iata, {"demand_kt": a.saf_demand_kt_per_year, "delivered_kt": 0})
        met = d["delivered_kt"] + 1e-3 >= d["demand_kt"]
        rows.append({
            "iata": a.iata,
            "name": a["name"],
            "lat": a.latitude,
            "lon": a.longitude,
            "demand_kt": d["demand_kt"],
            "delivered_kt": round(d["delivered_kt"], 1),
            "status": "✓ met" if met else "✗ short",
            "colour": COLOUR["airport"],
            "radius": 20000,
        })
    return pd.DataFrame(rows)


def _flow_arc_df(solution: Dict[str, Any],
                 biomass: pd.DataFrame,
                 airports: pd.DataFrame) -> pd.DataFrame:
    """Build one DataFrame of arcs with from/to coords and colour."""
    # Lookup tables
    biomass_coords = biomass.set_index("site_id")[["latitude", "longitude"]].to_dict("index")
    airport_coords = airports.set_index("iata")[["latitude", "longitude"]].to_dict("index")
    facility_coords = {f["id"]: (f["lat"], f["lon"]) for f in solution["facilities"]}

    def coords(node_id):
        if node_id in facility_coords:
            return facility_coords[node_id]
        if node_id in biomass_coords:
            b = biomass_coords[node_id]
            return (b["latitude"], b["longitude"])
        if node_id in airport_coords:
            a = airport_coords[node_id]
            return (a["latitude"], a["longitude"])
        return None

    rows = []
    for f in solution["flows"]:
        if f["tonnes"] < 1e-3:
            continue
        src = coords(f["from_id"])
        dst = coords(f["to_id"])
        if src is None or dst is None:
            continue
        if f["mode"] == "barge":
            colour = COLOUR["arc_barge"]
        elif f["stage"] == "farm_to_depot":
            colour = COLOUR["arc_biomass"]
        elif f["stage"] == "depot_to_refinery":
            colour = COLOUR["arc_intermediate"]
        else:
            colour = COLOUR["arc_saf"]
        rows.append({
            "from_lat": src[0], "from_lon": src[1],
            "to_lat": dst[0], "to_lon": dst[1],
            "tonnes": f["tonnes"],
            "stage": f["stage"],
            "mode": f["mode"],
            "colour": colour,
            # Width scaled by tonnes, clamped so refinery->airport doesn't dominate
            "width": min(8.0, max(0.6, f["tonnes"] / 50_000)),
        })
    return pd.DataFrame(rows)


def build_map(solution: Dict[str, Any],
              biomass: pd.DataFrame,
              airports: pd.DataFrame) -> pdk.Deck:
    biomass_layer = pdk.Layer(
        "ScatterplotLayer",
        data=_biomass_df(biomass, solution),
        get_position="[longitude, latitude]",
        get_fill_color="colour",
        get_radius="radius",
        pickable=True,
        opacity=0.7,
    )
    facility_layer = pdk.Layer(
        "ScatterplotLayer",
        data=_facility_marker_df(solution),
        get_position="[lon, lat]",
        get_fill_color="colour",
        get_radius="radius",
        pickable=True,
        stroked=True,
        get_line_color=[255, 255, 255],
        line_width_min_pixels=2,
    )
    airport_layer = pdk.Layer(
        "ScatterplotLayer",
        data=_airport_df(airports, solution),
        get_position="[lon, lat]",
        get_fill_color="colour",
        get_radius="radius",
        pickable=True,
        stroked=True,
        get_line_color=[255, 255, 255],
        line_width_min_pixels=2,
    )
    arc_df = _flow_arc_df(solution, biomass, airports)
    arc_layer = pdk.Layer(
        "ArcLayer",
        data=arc_df,
        get_source_position="[from_lon, from_lat]",
        get_target_position="[to_lon, to_lat]",
        get_source_color="colour",
        get_target_color="colour",
        get_width="width",
        pickable=True,
    )
    tooltip = {
        "html": "<b>{label}{iata}{site_id}</b><br/>"
                "{throughput_kt}{capacity_kt_per_year}{delivered_kt}",
        "style": {"backgroundColor": "#1e1e1e", "color": "white"}
    }
    return pdk.Deck(
        layers=[biomass_layer, arc_layer, facility_layer, airport_layer],
        initial_view_state=VIEW,
        map_style="mapbox://styles/mapbox/dark-v10",
        tooltip=tooltip,
    )


# ---------- charts ----------

def cost_breakdown_chart(solution: Dict[str, Any],
                         baseline_usd: float | None = None) -> go.Figure:
    """
    Stacked horizontal bar. If baseline_usd is given, add a second bar labelled
    'Import baseline' so judges see a before/after comparison.
    """
    cb = solution["cost_breakdown"]
    items = [
        ("Annualised CapEx", cb["capex_annualised_usd"], "#1f77b4"),
        ("Fixed OpEx",       cb["fixed_opex_usd"],       "#17becf"),
        ("Variable OpEx",    cb["variable_opex_usd"],    "#2ca02c"),
        ("Trucking",         cb["trucking_usd"],         "#ff7f0e"),
        ("Barge (TRK)",      cb["barge_usd"],            "#9467bd"),
    ]
    fig = go.Figure()
    for name, val, colour in items:
        fig.add_bar(
            y=["Optimised network"], x=[val / 1e6],
            name=name, orientation="h", marker_color=colour,
            hovertemplate=f"{name}: $%{{x:,.1f}}M<extra></extra>",
        )
    if baseline_usd is not None:
        fig.add_bar(
            y=["Status quo (import)"], x=[baseline_usd / 1e6],
            name="Import at parity", orientation="h",
            marker_color="#808080",
            hovertemplate="Import: $%{x:,.1f}M<extra></extra>",
        )
    fig.update_layout(
        barmode="stack",
        xaxis_title="USD, millions / year",
        yaxis_title="",
        height=220, margin=dict(l=10, r=10, t=10, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=-0.45),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def facility_table(solution: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for f in solution["facilities"]:
        rows.append({
            "ID": f["id"],
            "Type": f["type"].title(),
            "Size": f["size"],
            "Lat": round(f["lat"], 4),
            "Lon": round(f["lon"], 4),
            "Throughput (kt/yr)": round(f["throughput_kt_in"], 1),
            "Capacity (kt/yr)": round(f["capacity_kt_in"], 1),
            "Utilisation": f'{f["utilisation"]*100:.1f}%',
        })
    return pd.DataFrame(rows)


def flow_table(solution: Dict[str, Any], stage: str) -> pd.DataFrame:
    rows = []
    for f in solution["flows"]:
        if f["stage"] != stage or f["tonnes"] < 1e-3:
            continue
        rows.append({
            "From": f["from_id"],
            "To": f["to_id"],
            "Tonnes/yr": round(f["tonnes"], 0),
            "Distance (km)": round(f["distance_km"], 1),
            "Mode": f["mode"],
            "Cost (USD/yr)": round(f["cost_usd"], 0),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Tonnes/yr", ascending=False)
    return df
