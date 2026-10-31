"""
Map and coordinate table for the simplified dashboard.

- Base map: Carto (free, no API token).
- Tooltips: plain text with newlines rendered via CSS white-space: pre-line.
- Facility markers split by (type, size) so each has its own radius and
  pixel cap. Pixel caps prevent markers from engulfing the map at high zoom.
- Focus mode: when a facility is highlighted (via a row click in the UI),
  draw a gold ring around it and fade every unrelated marker and arc. The
  one-hop network — immediate upstream and downstream nodes — stays bright.
"""

from typing import Dict, Any, List, Set, Tuple, Optional
import pandas as pd
import pydeck as pdk


# Hue = type, saturation = size.
COLOUR = {
    "refinery_large": [178, 34, 52,  240],
    "refinery_small": [255, 120, 140, 220],
    "depot_large":    [255, 140, 0,   235],
    "depot_small":    [255, 195, 110, 215],
    "airport":        [30, 144, 255,  240],
    "biomass_used":   [34, 139, 34,   180],
    "biomass_unused": [120, 120, 120, 60],
    "arc_biomass":    [34, 139, 34,  220],
    "arc_pre":        [255, 140, 0,  220],
    "arc_saf":        [220, 20, 60,  220],
    "arc_barge":      [138, 43, 226, 230],
}

# radius_m scales with zoom until it hits max_px, after which the marker
# can't grow any larger on screen (prevents engulfing at high zoom).
MARKER = {
    "refinery_large": {"radius_m": 18000, "max_px": 60, "stroke_px": 3},
    "refinery_small": {"radius_m": 10000, "max_px": 35, "stroke_px": 2},
    "depot_large":    {"radius_m": 11000, "max_px": 36, "stroke_px": 2},
    "depot_small":    {"radius_m":  6000, "max_px": 20, "stroke_px": 1},
    "airport":        {"radius_m": 15000, "max_px": 44, "stroke_px": 3},
    "biomass":        {"max_px": 16, "min_px": 3},
}

VIEW = pdk.ViewState(latitude=0.0, longitude=114.0, zoom=5.3, pitch=35)

# Alpha multiplier applied to unrelated markers / arcs when focus is active.
FADE_FACTOR = 0.8


# ---------------------------------------------------------------------------
# Focus-mode helpers
# ---------------------------------------------------------------------------

def _fade(rgba: list, factor: float = FADE_FACTOR) -> list:
    """Return a new RGBA with alpha scaled by `factor`."""
    out = list(rgba)
    if len(out) == 3:
        out.append(255)
    out[3] = max(10, int(out[3] * factor))
    return out


def _related_sets(solution: Dict[str, Any],
                  highlight_id: Optional[str],
                  ) -> Tuple[Optional[Set[str]], Optional[Set[Tuple[str, str]]]]:
    """
    Compute one-hop neighbours of the highlighted facility.

    Returns (related_node_ids, related_arc_keys). Both are None when no
    highlight is active, meaning "don't fade anything".
    """
    if not highlight_id:
        return None, None
    nodes: Set[str] = {highlight_id}
    arcs: Set[Tuple[str, str]] = set()
    for f in solution["flows"]:
        if f["from_id"] == highlight_id or f["to_id"] == highlight_id:
            nodes.add(f["from_id"])
            nodes.add(f["to_id"])
            arcs.add((f["from_id"], f["to_id"]))
    return nodes, arcs


def _resolve_colour(base: list, this_id: str,
                    related: Optional[Set[str]]) -> list:
    """Apply fade if focus is active AND this_id isn't in the related set."""
    if related is None or this_id in related:
        return list(base)
    return _fade(base)


# ---------------------------------------------------------------------------
# Row builders (one per layer category)
# ---------------------------------------------------------------------------

def _facility_rows(solution: Dict[str, Any], facility_type: str, size: str,
                   related: Optional[Set[str]] = None) -> pd.DataFrame:
    key = f"{facility_type}_{size.lower()}"
    rows = []
    for f in solution["facilities"]:
        if f["type"] != facility_type or f["size"] != size:
            continue
        rows.append({
            "lat": f["lat"], "lon": f["lon"],
            "colour": _resolve_colour(COLOUR[key], f["id"], related),
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


def _biomass_rows(biomass: pd.DataFrame, solution: Dict[str, Any],
                  related: Optional[Set[str]] = None) -> pd.DataFrame:
    used = {f["from_id"] for f in solution["flows"]
            if f["stage"] == "farm_to_depot" and f["tonnes"] > 1}
    df = biomass.copy()
    df["used"] = df["site_id"].isin(used)
    df["colour"] = df.apply(
        lambda r: _resolve_colour(
            COLOUR["biomass_used"] if r.used else COLOUR["biomass_unused"],
            r.site_id, related,
        ), axis=1,
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


def _airport_rows(airports: pd.DataFrame, solution: Dict[str, Any],
                  related: Optional[Set[str]] = None) -> pd.DataFrame:
    delivery = {a["iata"]: a for a in solution["airport_delivery"]}
    rows = []
    for _, a in airports.iterrows():
        d = delivery.get(a.iata, {"demand_kt": a.saf_demand_kt_per_year,
                                   "delivered_kt": 0})
        met = d["delivered_kt"] + 1e-3 >= d["demand_kt"]
        rows.append({
            "lat": a.latitude, "lon": a.longitude,
            "colour": _resolve_colour(COLOUR["airport"], a.iata, related),
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
              airports: pd.DataFrame,
              related_arcs: Optional[Set[Tuple[str, str]]] = None,
              ) -> pd.DataFrame:
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
            base, material = COLOUR["arc_barge"], "SAF (barge)"
        elif f["stage"] == "farm_to_depot":
            base, material = COLOUR["arc_biomass"], "biomass"
        elif f["stage"] == "depot_to_refinery":
            base, material = COLOUR["arc_pre"], "biomass"
        else:
            base, material = COLOUR["arc_saf"], "SAF"
        arc_key = (f["from_id"], f["to_id"])
        if related_arcs is None or arc_key in related_arcs:
            colour = list(base)
            width = min(8.0, max(0.6, f["tonnes"] / 50_000))
        else:
            colour = _fade(base)
            width = min(3.0, max(0.4, f["tonnes"] / 100_000))
        kt = f["tonnes"] / 1000.0
        rows.append({
            "from_lat": src[0], "from_lon": src[1],
            "to_lat": dst[0],   "to_lon": dst[1],
            "colour": colour, "width": width,
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
              airports: pd.DataFrame,
              highlight_id: Optional[str] = None) -> pdk.Deck:
    """
    Build the Deck. If highlight_id matches a facility, draw a gold ring
    around it and fade every unrelated marker/arc so the one-hop network
    stands out.
    """
    related_nodes, related_arcs = _related_sets(solution, highlight_id)

    layers: List[pdk.Layer] = []

    # Biomass behind arcs
    bio_df = _biomass_rows(biomass, solution, related=related_nodes)
    bio_df = bio_df.rename(columns={"latitude": "lat", "longitude": "lon"})
    layers.append(_scatter(
        bio_df,
        max_px=MARKER["biomass"]["max_px"],
        min_px=MARKER["biomass"]["min_px"],
    ))

    # Arcs
    layers.append(pdk.Layer(
        "ArcLayer",
        data=_arc_rows(solution, biomass, airports, related_arcs=related_arcs),
        get_source_position="[from_lon, from_lat]",
        get_target_position="[to_lon, to_lat]",
        get_source_color="colour", get_target_color="colour",
        get_width="width", pickable=True,
    ))

    # Facilities: Small on bottom, Large on top
    for ftype, fsize in (("depot", "Small"), ("depot", "Large"),
                         ("refinery", "Small"), ("refinery", "Large")):
        df = _facility_rows(solution, ftype, fsize, related=related_nodes)
        if df.empty:
            continue
        key = f"{ftype}_{fsize.lower()}"
        layers.append(_scatter(
            df,
            max_px=MARKER[key]["max_px"],
            stroke_px=MARKER[key]["stroke_px"],
        ))

    # Airports on top
    layers.append(_scatter(
        _airport_rows(airports, solution, related=related_nodes),
        max_px=MARKER["airport"]["max_px"],
        stroke_px=MARKER["airport"]["stroke_px"],
    ))

    # Highlight ring last so it sits above everything
    if highlight_id:
        target = next(
            (f for f in solution["facilities"] if f["id"] == highlight_id),
            None,
        )
        if target is not None:
            key = f"{target['type']}_{target['size'].lower()}"
            base_radius = MARKER[key]["radius_m"]
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=pd.DataFrame([{
                    "lat": target["lat"], "lon": target["lon"],
                    "tooltip": f"★ Selected: {target['id']}",
                }]),
                get_position="[lon, lat]",
                get_fill_color=[0, 0, 0, 0],
                get_radius=base_radius * 1.6,
                radius_max_pixels=80,
                radius_min_pixels=10,
                stroked=True,
                get_line_color=[255, 215, 0],
                line_width_min_pixels=4,
                pickable=False,
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
# Tables for the dashboard
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
