"""
SAF Supply Chain Optimiser — dashboard.

Run with:  streamlit run app.py

Wraps the real MILP (saf package) via src/adapter.py. On cold start it loads
data/cached_solution.json if it exists, so the dashboard renders instantly.
The 'Re-solve' button in the sidebar runs the solver fresh (~60 s with HiGHS).
"""

import json
from pathlib import Path

import streamlit as st
import pandas as pd

from src.data_loader import load_airports, load_biomass
from src.adapter import (
    solve, save_solution, load_cached_solution, load_run_py_results,
    CACHE_PATH,
)
from src.schema import validate_solution
from src.visualizer import (
    build_map, cost_breakdown_chart, facility_table, flow_table,
)

RESULTS_JSON_PATH = Path(__file__).resolve().parent / "results.json"

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="SAF Supply Chain Optimiser",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Autonomous SG 2026")
    st.markdown("**Challenge 3 — SAF Supply Chain, Kalimantan**")
    st.caption(
        "User: SAF offtake planner at an Indonesian carrier. "
        "Today: no domestic SAF, imports at ~$1.80/L. "
        "Goal: minimum-cost 4-airport network meeting 1,200 kt/yr demand."
    )
    st.divider()
    st.markdown("#### Solver")
    time_limit = st.slider("Solver time limit (s)", 30, 600, 180, 30)
    mip_gap = st.select_slider(
        "MIP gap tolerance",
        options=[0.0001, 0.001, 0.005, 0.01, 0.05],
        value=0.001, format_func=lambda g: f"{g*100:.2f}%",
    )
    include_airports = st.checkbox(
        "Include airports as candidate facility sites", value=True,
        help="If off: ablation — depots and refineries can only open at "
             "biomass sites. Lets you demo the sensitivity of the design.",
    )
    st.divider()
    col_a, col_b = st.columns(2)
    with col_a:
        run_btn = st.button("▶ Re-solve", type="primary", use_container_width=True)
    with col_b:
        reload_btn = st.button("↻ Reload cache", use_container_width=True)
    st.caption(
        "Cold start loads cached_solution.json if present. "
        "Re-solve rebuilds and solves the MILP."
    )

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
col_title, col_badge = st.columns([3, 1])
with col_title:
    st.title("SAF Supply Chain Optimiser")
    st.caption(
        "Siting depots and refineries in Kalimantan to meet jet-fuel demand "
        "at PNK, PKN, BPN and TRK. Minimises annualised CapEx + OpEx + logistics."
    )
with col_badge:
    st.markdown(
        "<div style='text-align:right; padding-top:1rem;'>"
        "<span style='background:#1f77b4;color:white;padding:0.25rem 0.6rem;"
        "border-radius:4px;font-size:0.8rem;'>Autonomous SG 2026</span></div>",
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Load / solve
# ---------------------------------------------------------------------------
def _initial_solution():
    # Priority 1: results.json from a prior `python run.py`
    if RESULTS_JSON_PATH.exists():
        try:
            return load_run_py_results(RESULTS_JSON_PATH), "results.json (from run.py)"
        except Exception:
            pass
    # Priority 2: cached_solution.json saved by a previous dashboard run
    cached = load_cached_solution()
    if cached is not None:
        return cached, "data/cached_solution.json"
    return None, None

if "solution" not in st.session_state:
    sol, source = _initial_solution()
    if sol is not None:
        st.session_state.solution = sol
        st.session_state.solution_source = source

if reload_btn:
    sol, source = _initial_solution()
    if sol is not None:
        st.session_state.solution = sol
        st.session_state.solution_source = source
        st.toast(f"Reloaded from {source}", icon="↻")

if run_btn or "solution" not in st.session_state:
    with st.status("Solving MILP…", expanded=True) as status_box:
        st.write("Building model…")
        try:
            new_sol = solve(
                time_limit=time_limit,
                mip_gap=mip_gap,
                include_airport_candidates=include_airports,
                verbose=False,
            )
            save_solution(new_sol)
            st.session_state.solution = new_sol
            st.session_state.solution_source = "fresh solve"
            status_box.update(
                label=f"Solved in {new_sol['solve_time_sec']:.1f}s · "
                      f"${new_sol['objective_value_usd']/1e6:,.0f}M/yr",
                state="complete",
            )
        except Exception as e:
            status_box.update(label=f"Solver error: {e}", state="error")
            st.stop()

solution = st.session_state.solution
source = st.session_state.get("solution_source", "unknown")

airports = load_airports()
biomass = load_biomass()

# ---------------------------------------------------------------------------
# Status + headline metrics
# ---------------------------------------------------------------------------
status = solution["status"]
if status != "optimal":
    st.error(f"Solver status: **{status}**. See diagnostics below.")
else:
    st.success(
        f"Solved in {solution['solve_time_sec']:.1f}s · "
        f"optimality gap {solution['optimality_gap']*100:.2f}% · "
        f"source: *{source}*"
    )

total = solution["cost_breakdown"]["total_usd"]
depots = [f for f in solution["facilities"] if f["type"] == "depot"]
refineries = [f for f in solution["facilities"] if f["type"] == "refinery"]
demand_met = all(
    a["delivered_kt"] + 1e-3 >= a["demand_kt"]
    for a in solution["airport_delivery"]
)
total_biomass_kt = sum(
    f["tonnes"] for f in solution["flows"] if f["stage"] == "farm_to_depot"
) / 1000.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total annual cost", f"${total/1e6:,.1f}M")
m2.metric("Facilities built", f"{len(depots)}D + {len(refineries)}R")
m3.metric("Biomass used", f"{total_biomass_kt:,.0f} kt/yr",
          help="Of the 6,800 kt available.")
m4.metric("Demand met", "4 / 4 ✓" if demand_met else "✗ shortfall",
          delta=None if demand_met else "underserved",
          delta_color="inverse")

# ---------------------------------------------------------------------------
# Main row: map + cost
# ---------------------------------------------------------------------------
map_col, side_col = st.columns([3, 2])

with map_col:
    st.markdown("##### Network map")
    st.pydeck_chart(build_map(solution, biomass, airports), use_container_width=True)
    st.caption(
        "🟢 Biomass sites (grey = unused) · 🟠 Depots · 🔴 Refineries · 🔵 Airports · "
        "Arcs: green = biomass haul, orange = pre-processed, red = SAF road, "
        "purple = SAF barge to Tarakan."
    )

with side_col:
    st.markdown("##### Cost breakdown")
    baseline = 1_200_000 * 1800  # 1,200 kt × ~$1,800/t import parity
    st.plotly_chart(
        cost_breakdown_chart(solution, baseline_usd=baseline),
        use_container_width=True,
    )
    saving = baseline - total
    pct = saving / baseline * 100 if baseline else 0
    st.metric("Saving vs import baseline",
              f"${saving/1e6:,.0f}M / yr",
              delta=f"{pct:.1f}%")

    st.markdown("##### Airport demand")
    ad = pd.DataFrame(solution["airport_delivery"])
    ad["status"] = ad.apply(
        lambda r: "✓" if r.delivered_kt + 1e-3 >= r.demand_kt else "✗",
        axis=1,
    )
    ad["sources"] = ad["sources"].apply(lambda s: ", ".join(s))
    st.dataframe(
        ad[["iata", "demand_kt", "delivered_kt", "status", "sources"]],
        hide_index=True, use_container_width=True,
    )

# ---------------------------------------------------------------------------
# Detail tabs
# ---------------------------------------------------------------------------
tab_fac, tab_flow, tab_diag = st.tabs(
    ["Facilities", "Material flows", "Diagnostics & why"]
)

with tab_fac:
    st.markdown("##### Facility decisions")
    st.dataframe(facility_table(solution), hide_index=True, use_container_width=True)
    st.caption(f"{len(depots)} depots and {len(refineries)} refineries opened.")

with tab_flow:
    stage = st.radio(
        "Stage", ["farm_to_depot", "depot_to_refinery", "refinery_to_airport"],
        format_func=lambda s: s.replace("_", " → ").title(),
        horizontal=True,
    )
    df = flow_table(solution, stage)
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.caption(f"{len(df)} flows at this stage.")

with tab_diag:
    st.markdown("##### Checks")
    errs = validate_solution(solution)
    if errs:
        for e in errs:
            st.warning(e)
    else:
        st.success("No consistency issues flagged by the dashboard validator.")
    if solution.get("notes"):
        st.caption(f"**Solver notes:** {solution['notes']}")

    st.markdown("##### Objective")
    st.code(
        "min Σ (annualised_CapEx_d · y_dep[d,s])\n"
        "  + Σ (annualised_CapEx_r · y_ref[r,s])\n"
        "  + Σ fixed_OpEx · y[.,.]\n"
        "  + Σ var_OpEx · intake\n"
        "  + Σ truck_rate_leg · distance_leg · tonnes_leg\n"
        "  + Σ barge_rate · distance · tonnes            (TRK leg)\n"
        "s.t.  demand_a met at each airport (equality),\n"
        "      depot mass balance (lossless),\n"
        "      refinery yield 0.20 t SAF / t biomass,\n"
        "      capacity ≤ Large or Small per facility,\n"
        "      farm supply ≤ 80-site availability.",
        language="text",
    )

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
st.divider()
exp1, exp2, exp3, _ = st.columns([1, 1, 1, 3])
with exp1:
    st.download_button(
        "⬇ facilities.csv",
        facility_table(solution).to_csv(index=False).encode(),
        file_name="facilities.csv", mime="text/csv",
    )
with exp2:
    all_flows = pd.concat([
        flow_table(solution, s) for s in
        ("farm_to_depot", "depot_to_refinery", "refinery_to_airport")
    ], ignore_index=True)
    st.download_button(
        "⬇ flows.csv",
        all_flows.to_csv(index=False).encode(),
        file_name="flows.csv", mime="text/csv",
    )
with exp3:
    st.download_button(
        "⬇ solution.json",
        json.dumps(solution, indent=2).encode(),
        file_name="solution.json", mime="application/json",
    )
