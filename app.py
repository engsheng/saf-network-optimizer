"""
SAF Supply Chain Optimiser — dashboard.

Four things on one page:
  1. Total annual cost
  2. Facility counts by type and size
  3. Kalimantan map with hover-tooltips on arrows (showing material + tonnes)
  4. Coordinates table

Run:  python -m streamlit run app.py
"""

from pathlib import Path
import streamlit as st
import pandas as pd

from src.data_loader import load_airports, load_biomass
from src.adapter import solve, save_solution, load_cached_solution, load_run_py_results
from src.visualizer import build_map, facility_counts, coordinates_table

RESULTS_JSON_PATH = Path(__file__).resolve().parent / "results.json"

st.set_page_config(
    page_title="SAF Supply Chain Optimiser",
    page_icon="✈️",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Load (or solve once on first run)
# ---------------------------------------------------------------------------
def _get_solution():
    if "solution" in st.session_state:
        return st.session_state.solution
    # Fast paths: pre-solved results from CLI, or cache from a previous dashboard run
    if RESULTS_JSON_PATH.exists():
        try:
            st.session_state.solution = load_run_py_results(RESULTS_JSON_PATH)
            return st.session_state.solution
        except Exception:
            pass
    cached = load_cached_solution()
    if cached is not None:
        st.session_state.solution = cached
        return cached
    # First-run cold solve
    with st.status(
        "Solving the optimisation model… this happens once (about a minute). "
        "After this, the dashboard loads instantly.",
        expanded=False,
    ):
        new_sol = solve(time_limit=180, mip_gap=0.001)
        save_solution(new_sol)
    st.session_state.solution = new_sol
    return new_sol


solution = _get_solution()
airports = load_airports()
biomass = load_biomass()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("SAF Supply Chain Optimiser — Kalimantan")
st.caption(
    "Siting depots and refineries across Kalimantan to meet 1,200 kt·yr of "
    "SAF demand at PNK, PKN, BPN and TRK."
)

# ---------------------------------------------------------------------------
# 1. Total cost
# ---------------------------------------------------------------------------
total = solution["cost_breakdown"]["total_usd"]
st.markdown("### Minimum total annual cost")
st.markdown(
    f"<div style='font-size:3.2rem;font-weight:700;color:#1f77b4;"
    f"line-height:1.1;margin:0.2rem 0 0.8rem 0;'>"
    f"${total/1e6:,.1f} M <span style='font-size:1.2rem;font-weight:400;"
    f"color:#888;'>per year</span></div>",
    unsafe_allow_html=True,
)

# Compact breakdown: one thin stacked bar + a single legend line
cb = solution["cost_breakdown"]
parts = [
    ("Capex (annualised)", cb["capex_annualised_usd"], "#1f77b4"),
    ("Fixed O&M",          cb["fixed_opex_usd"],       "#6baed6"),
    ("Variable O&M",       cb["variable_opex_usd"],    "#ff7f0e"),
    ("Trucking",           cb["trucking_usd"],         "#2ca02c"),
    ("Barge",              cb["barge_usd"],            "#9467bd"),
]
parts = [p for p in parts if p[1] > 0]
denom = sum(v for _, v, _ in parts) or 1.0
bar = "".join(
    f"<div title='{name}: ${v/1e6:,.1f} M' "
    f"style='width:{v/denom*100:.2f}%;background:{c};'></div>"
    for name, v, c in parts
)
legend = " &nbsp;·&nbsp; ".join(
    f"<span style='color:{c};'>■</span> {name} "
    f"<b>${v/1e6:,.1f} M</b> <span style='color:#888;'>({v/denom:.0%})</span>"
    for name, v, c in parts
)
st.markdown(
    f"<div style='display:flex;height:8px;border-radius:4px;overflow:hidden;"
    f"max-width:900px;margin-bottom:0.4rem;'>{bar}</div>"
    f"<div style='font-size:0.85rem;margin-bottom:0.8rem;'>{legend}</div>",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# 2. Facility counts
# ---------------------------------------------------------------------------
counts = facility_counts(solution)
dep_L = counts.get(("depot", "Large"), 0)
dep_S = counts.get(("depot", "Small"), 0)
ref_L = counts.get(("refinery", "Large"), 0)
ref_S = counts.get(("refinery", "Small"), 0)

st.markdown("### Facilities built")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Large depots",     dep_L)
c2.metric("Small depots",     dep_S)
c3.metric("Large refineries", ref_L)
c4.metric("Small refineries", ref_S)

# ---------------------------------------------------------------------------
# 3. Map
# ---------------------------------------------------------------------------
st.markdown("### Network map")
st.caption(
    "🟢 Biomass sites · 🟠 Depots · 🔴 Refineries · 🔵 Airports. "
    "Hover any arrow to see material and tonnes per year. "
    "Purple arrows are SAF barged to Tarakan."
)
st.pydeck_chart(build_map(solution, biomass, airports), use_container_width=True)

# ---------------------------------------------------------------------------
# 4. Coordinates
# ---------------------------------------------------------------------------
st.markdown("### Facility coordinates")
st.caption("Refineries first, then depots by throughput.")
st.dataframe(
    coordinates_table(solution),
    hide_index=True,
    use_container_width=True,
)
