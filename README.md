# SAF Supply Chain Optimizer

**Autonomous SG 2026 · Airbus Challenge #3** — a mixed-integer linear program
that places depots and refineries across Kalimantan to feed Sustainable
Aviation Fuel (SAF) to four airports at minimum total annual cost.

Design docs:
- [Design Approach](https://claude.ai/artifact/EApVyiiWJWf8J74AaNH3Qu) — the core economic insight (refineries belong near depots, not near airports) and expected solution structure
- [Modeling Assumptions](https://claude.ai/artifact/2xDHn7mQdNdtQ7orN2umQZ) — every parameter choice, including why altitude is excluded

---

## What it does

Given

- 80 biomass sites across Kalimantan (6,800 kt available per year)
- 4 airports each requiring 300 kt of SAF per year (1,200 kt total)
- Fixed CapEx / OpEx / capacity for Depot and Refinery facilities in two sizes
- Transport rates for farm→depot, depot→refinery, refinery→airport

the model decides

- which candidate sites to open as depots and refineries, in which size
- how much biomass flows from each farm to each depot
- how much biomass flows from each depot to each refinery
- how much SAF flows from each refinery to each airport

so that every airport's demand is met and total annual cost is minimised.

Total annual cost =
`annualised CapEx + fixed OpEx + variable OpEx + logistics`.

CapEx is amortised straight-line over 20 years with zero interest, per the
organizer's clarifications.

---

## Core insight that drives the solution

Transport cost **per tonne of final SAF per kilometre**:

| Leg | Rate | Mass per t SAF | USD/km per t SAF | vs cheapest |
| --- | --- | --- | --- | --- |
| Farm → Depot | 0.10 USD/t·km | 5 t (biomass) | 0.50 | 6.25× |
| Depot → Refinery | 0.12 USD/t·km | 5 t (biomass) | **0.60** | **7.5×** |
| Refinery → Airport | 0.08 USD/t·km | 1 t (SAF) | 0.08 | 1× |

The depot-to-refinery leg is **7.5× more expensive per unit of final SAF**
than refinery-to-airport, because biomass is five tonnes per tonne of SAF
(yield η = 0.20) *and* the biomass-leg rate is already 50% higher. So the
optimal placement pulls refineries toward depot clusters, not toward
airports. See the [Design Approach](https://claude.ai/artifact/EApVyiiWJWf8J74AaNH3Qu)
doc for the full argument and expected solution patterns.

---

## Install

```bash
git clone https://github.com/engsheng/saf-network-optimizer.git
cd saf-network-optimizer
pip install -r requirements.txt
```

PuLP 2.9 ships with CBC bundled, so this install alone gives you a working
solver. For 5–10× faster MILP solves, install `highspy` as well
(it's in `requirements.txt`); the runner uses HiGHS if available and silently
falls back to CBC otherwise.

---

## Run

Drop the organizer's biomass CSV at `data/biomass.csv`, then:

```bash
python run.py
```

Without that file, the runner uses an 80-site synthetic layout for smoke
testing and prints a warning.

Common options:

```bash
# Faster solve, 2% MIP gap
python run.py --mip-gap 0.02 --time-limit 300

# CBC only (no HiGHS)
python run.py --solver cbc

# Ablation: exclude airports from candidate sites
python run.py --no-airport-candidates

# Full HiGHS log for debugging
python run.py              # (quiet is OFF by default; add --quiet to suppress)

# Multi-thread HiGHS
python run.py --threads 4
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | solved, all hard validation checks pass |
| 1 | solved but at least one hard validation check failed |
| 2 | solver returned no incumbent, or model infeasible |
| 3 | input file missing or malformed |

---

## Smoke test

```bash
python tests/test_smoke.py
```

Uses the synthetic 80-farm layout with a 10% gap tolerance so it runs
quickly in CI. Asserts: distances compute correctly, all four airports
receive exactly 300 kt, system-wide mass balance holds (6,000 kt biomass
in → 1,200 kt SAF out at 0.20 yield), and no hard validation checks fire.

---

## Formulation (short form)

**Candidate locations** `J` : the 80 biomass sites plus the 4 airports (84 points).

**Decision variables**

| Variable | Type | Meaning |
| --- | --- | --- |
| `y_dep[j, s_D]` | binary | open a depot of size s_D at candidate j |
| `y_ref[j, s_R]` | binary | open a refinery of size s_R at candidate j |
| `x[f, j, s_D]` | ≥ 0 | biomass flow (kt/yr) farm f → depot j of size s_D |
| `w[d, r, s_R]` | ≥ 0 | biomass flow depot d → refinery r of size s_R |
| `z[r, a, s_R]` | ≥ 0 | SAF flow refinery r of size s_R → airport a |

Size-indexing `x` and `w` keeps the per-tonne variable OpEx term linear
(the rate differs by size: 5 vs 4 for depots, 60 vs 55 for refineries).

**Objective** (USD / year)

```
min  Σ annualised_capex(j,s) · y_dep[j,s]  +  Σ annualised_capex(j,s) · y_ref[j,s]
   + Σ fixed_opex(s) · y_dep[j,s]          +  Σ fixed_opex(s) · y_ref[j,s]
   + Σ var_opex_dep(s) · 1000 · x[f,d,s]
   + Σ var_opex_ref(s) · 1000 · w[d,r,s]
   + Σ 0.10 · 1000 · d_FD(f,d) · x[f,d,s]
   + Σ 0.12 · 1000 · d_DR(d,r) · w[d,r,s]
   + Σ 0.08 · 1000 · d_RA(r,a) · z[r,a,s]
```

(The `× 1000` converts kt flows to tonnes to match per-tonne cost rates.)

**Constraints**

1. Farm supply (soft): `Σ x[f,d,s] ≤ biomass_f`
2. One size per candidate: `Σ_s y_dep[j,s] ≤ 1`, same for `y_ref`
3. Depot capacity binds flow to a size: `Σ_f x[f,d,s] ≤ cap_dep(s) · y_dep[d,s]`
4. Depot mass balance (lossless): `Σ_f Σ_s x[f,d,s] = Σ_r Σ_s' w[d,r,s']`
5. Refinery capacity: `Σ_d w[d,r,s] ≤ cap_ref(s) · y_ref[r,s]`
6. Refinery yield: `0.20 · Σ_d w[d,r,s] = Σ_a z[r,a,s]`
7. Airport demand (hard equality): `Σ_r Σ_s z[r,a,s] = demand_a`

**Distances**: haversine (great-circle) × 1.3 road factor. Same formula for
the Tarakan barge leg, at the same 0.08 USD/t·km rate as road refinery-to-airport
deliveries. Altitude is deliberately excluded — see the Assumptions doc.

---

## Repository layout

```
saf-network-optimizer/
├── README.md
├── requirements.txt
├── run.py                  entry-point CLI
├── saf/
│   ├── __init__.py
│   ├── distance.py         haversine × 1.3
│   ├── data.py             loaders + synthetic biomass fallback
│   ├── model.py            the MILP (PuLP)
│   ├── validate.py         hard checks + insight warnings
│   └── report.py           human summary + JSON extract
├── tests/
│   └── test_smoke.py       end-to-end on synthetic data
└── data/
    ├── airports.csv        4-airport reference set
    └── parameters.xlsx     facilities, transport, assumptions
```

Place the biomass CSV at `data/biomass.csv` for real runs; otherwise the
synthetic fallback is used (and clearly flagged in the output).

---

## Validation checks

After solving, the runner asserts:

1. All flow variables non-negative
2. Farm supply never exceeded
3. Depot mass balance (lossless): intake = outflow at every open depot
4. Refinery yield: 0.20 × biomass intake = SAF outflow at every refinery
5. Facility capacity respected at every open facility
6. Every airport receives exactly its 300 kt demand

Separately, an **insight warning** flags any refinery opened at an airport
candidate — the 7.5× cost argument predicts this should be rare, so the
warning is a prompt to inspect the biomass distribution around that airport,
not an error.

---

## License

Public hackathon code; see `LICENSE` (to be added).
