"""
Moulding Department — True Cost Calculator
A comprehensive financial model to determine the all-in cost per linear foot
of operating the custom moulding department (Van Nuys ↔ Gardena).
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(
    page_title="Moulding True Cost Calculator",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─── Style ────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Section headers */
    .sec-header {
        background: linear-gradient(90deg, #1b3a5c, #2d5f8a);
        color: white;
        padding: 7px 14px;
        border-radius: 5px;
        font-size: 0.88rem;
        font-weight: 700;
        letter-spacing: 0.04em;
        margin-bottom: 10px;
    }
    /* Subtotal bar */
    .subtotal {
        background-color: #eef3f9;
        border-left: 5px solid #2d5f8a;
        padding: 6px 14px;
        border-radius: 3px;
        font-size: 0.9rem;
        font-weight: 700;
        color: #1b3a5c;
        margin-top: 8px;
    }
    /* Result card */
    .result-card {
        background: #1b3a5c;
        color: white;
        border-radius: 8px;
        padding: 18px 22px;
        text-align: center;
    }
    .result-card .label { font-size: 0.78rem; opacity: 0.8; margin-bottom: 4px; }
    .result-card .value { font-size: 1.9rem; font-weight: 800; }
    .result-card .sub   { font-size: 0.82rem; opacity: 0.75; margin-top: 3px; }
    /* Table alternating rows */
    div[data-testid="stDataFrame"] table tbody tr:nth-child(even)
        { background-color: #f4f7fb; }
    /* Tight number inputs */
    div[data-testid="stNumberInput"] { margin-bottom: 2px; }
    /* Divider color */
    hr { border-color: #d0dce8; }
</style>
""", unsafe_allow_html=True)

# ─── Header ───────────────────────────────────────────────────────────────────
st.markdown("# Moulding Department — True Cost Model")
st.caption(
    "All inputs are **monthly** unless noted. "
    "Placeholder defaults are illustrative — replace every field with your actual figures. "
    "The model recomputes instantly as you edit."
)
st.divider()

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 0 — PRODUCTION VOLUME BASIS
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown('<div class="sec-header">SECTION 0 — PRODUCTION VOLUME BASIS</div>', unsafe_allow_html=True)
st.caption("These figures are the denominators for every per-unit calculation below.")

c0a, c0b, c0c, c0d = st.columns(4)
with c0a:
    monthly_lf = st.number_input(
        "Target sellable output (linear feet / month)",
        min_value=0.0, value=5_000.0, step=100.0, format="%.0f",
        help="The LF you actually ship/sell each month — after all defects resolved.",
    )
with c0b:
    sku_count = st.number_input(
        "Distinct SKU formulas run per month",
        min_value=0, value=15, step=1,
    )
with c0c:
    runs_per_month = st.number_input(
        "Production runs (round-trips) per month",
        min_value=0.0, value=8.0, step=1.0,
        help="Each 'run' = one Van Nuys → Gardena → Van Nuys trip.",
    )
with c0d:
    rework_rate = st.number_input(
        "Rework rate (% of production LF)",
        min_value=0.0, max_value=100.0, value=8.0, step=0.5, format="%.1f",
        help="Pieces that fail QC and return to Gardena for re-processing.",
    ) / 100.0
    writeoff_rate = st.number_input(
        "Write-off rate (% of production LF)",
        min_value=0.0, max_value=100.0, value=2.0, step=0.5, format="%.1f",
        help="Pieces that are unrecoverable — fully scrapped.",
    ) / 100.0

# Derived constants used throughout
WEEKS_PER_MONTH = 4.333
rework_lf       = monthly_lf * rework_rate      # LF that go through production twice
writeoff_lf     = monthly_lf * writeoff_rate     # LF completely lost
gross_lf        = monthly_lf * (1.0 + rework_rate + writeoff_rate)  # total LF processed

st.divider()
costs: dict[str, float] = {}   # populated section by section

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — DIRECT LABOR
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 1 — DIRECT LABOR", expanded=True):
    st.markdown('<div class="sec-header">1.1  Gardena Production Workers</div>', unsafe_allow_html=True)
    c1a, c1b, c1c = st.columns([1, 1, 1])
    with c1a:
        prod_workers        = st.number_input("Number of production workers",               min_value=0,   value=3,    step=1)
        prod_wage           = st.number_input("Average hourly wage — production ($)",        min_value=0.0, value=20.0, step=0.50)
        prod_hours_pw       = st.number_input("Hours per worker per week",                   min_value=0.0, value=40.0, step=1.0)
    prod_labor = prod_workers * prod_wage * prod_hours_pw * WEEKS_PER_MONTH
    with c1c:
        st.metric("Production labor", f"${prod_labor:,.2f} / mo")

    st.markdown('<div class="sec-header">1.2  Supervisor / Floor Lead (Gardena)</div>', unsafe_allow_html=True)
    c1a, c1b, c1c = st.columns([1, 1, 1])
    with c1a:
        sup_hours_pw = st.number_input("Supervisor hours per week",  min_value=0.0, value=20.0, step=1.0)
        sup_wage     = st.number_input("Supervisor hourly rate ($)", min_value=0.0, value=28.0, step=0.50)
    sup_labor = sup_wage * sup_hours_pw * WEEKS_PER_MONTH
    with c1c:
        st.metric("Supervisor labor", f"${sup_labor:,.2f} / mo")

    st.markdown('<div class="sec-header">1.3  Artisan — Color Formula Development</div>', unsafe_allow_html=True)
    c1a, c1b, c1c = st.columns([1, 1, 1])
    with c1a:
        artisan_hrs_per_formula = st.number_input("Artisan hours per formula / color match",          min_value=0.0, value=2.0,  step=0.25)
        artisan_rate            = st.number_input("Artisan hourly rate ($)",                           min_value=0.0, value=65.0, step=1.0)
    with c1b:
        artisan_formula_revisions = st.number_input("Additional revision sessions per month (all SKUs)", min_value=0.0, value=3.0, step=1.0,
                                                     help="Batches where the formula needs a tweak after the first run.")
    artisan_labor = artisan_rate * ((artisan_hrs_per_formula * sku_count) + (artisan_hrs_per_formula * artisan_formula_revisions))
    with c1c:
        st.metric("Artisan labor", f"${artisan_labor:,.2f} / mo")

    st.markdown('<div class="sec-header">1.4  QC Inspection — Van Nuys</div>', unsafe_allow_html=True)
    c1a, c1b, c1c = st.columns([1, 1, 1])
    with c1a:
        qc_hours = st.number_input("QC inspector hours per month",    min_value=0.0, value=40.0, step=1.0)
        qc_wage  = st.number_input("QC inspector hourly rate ($)",    min_value=0.0, value=22.0, step=0.50)
    qc_labor = qc_hours * qc_wage
    with c1c:
        st.metric("QC labor", f"${qc_labor:,.2f} / mo")

    st.markdown('<div class="sec-header">1.5  Receiving & Put-Away — Van Nuys (moulding only)</div>', unsafe_allow_html=True)
    c1a, c1b, c1c = st.columns([1, 1, 1])
    with c1a:
        recv_hours = st.number_input("Receiving / put-away hours per month", min_value=0.0, value=20.0, step=1.0)
        recv_wage  = st.number_input("Receiving labor hourly rate ($)",       min_value=0.0, value=19.0, step=0.50)
    recv_labor = recv_hours * recv_wage
    with c1c:
        st.metric("Receiving labor", f"${recv_labor:,.2f} / mo")

    st.markdown('<div class="sec-header">1.6  Rework Labor — Gardena (re-sand, re-stain, re-finish)</div>', unsafe_allow_html=True)
    c1a, c1b, c1c = st.columns([1, 1, 1])
    with c1a:
        rework_labor_per_lf = st.number_input(
            "Additional labor cost to rework ($ / linear foot re-processed)",
            min_value=0.0, value=0.35, step=0.01,
            help="Marginal cost only — the base production labor is already captured in 1.1.",
        )
    rework_labor = rework_lf * rework_labor_per_lf
    with c1c:
        st.metric("Rework labor", f"${rework_labor:,.2f} / mo", help=f"{rework_lf:,.0f} LF reworked")

    labor_total = prod_labor + sup_labor + artisan_labor + qc_labor + recv_labor + rework_labor
    costs["1. Direct Labor"] = labor_total
    st.markdown(f'<div class="subtotal">Section 1 Subtotal — Direct Labor: ${labor_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — RAW & PROCESS MATERIALS
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 2 — RAW & PROCESS MATERIALS", expanded=False):
    st.caption(
        f"Per-LF rates apply to **gross LF processed** ({gross_lf:,.0f} LF/mo = "
        f"{monthly_lf:,.0f} sellable + {rework_lf:,.0f} rework + {writeoff_lf:,.0f} write-off)."
    )
    c2a, c2b, c2c = st.columns([1, 1, 1])
    with c2a:
        raw_cost_lf      = st.number_input("Raw moulding profile ($ / LF)",                 min_value=0.0, value=0.45, step=0.01)
        stain_cost_lf    = st.number_input("Stain / dye ($ / LF)",                          min_value=0.0, value=0.08, step=0.01)
        sealer_cost_lf   = st.number_input("Sealer / primer coat ($ / LF)",                 min_value=0.0, value=0.05, step=0.01)
        topcoat_cost_lf  = st.number_input("Topcoat / finish ($ / LF)",                     min_value=0.0, value=0.07, step=0.01)
    with c2b:
        abrasive_cost_lf = st.number_input("Abrasives / sandpaper ($ / LF)",                min_value=0.0, value=0.03, step=0.005, format="%.3f")
        consumable_lf    = st.number_input("Consumables — rags, tape, applicators ($ / LF)",min_value=0.0, value=0.02, step=0.005, format="%.3f")
        tint_per_formula = st.number_input("Tints & pigments per formula run ($)",           min_value=0.0, value=15.0, step=1.0)
        test_piece_cost  = st.number_input("Test-piece material per new formula ($)",        min_value=0.0, value=8.0,  step=1.0)

    per_lf_rate      = raw_cost_lf + stain_cost_lf + sealer_cost_lf + topcoat_cost_lf + abrasive_cost_lf + consumable_lf
    mat_per_lf_cost  = per_lf_rate * gross_lf
    formula_mat_cost = (tint_per_formula + test_piece_cost) * sku_count
    # Write-off material loss already in gross_lf calc, but surface it explicitly
    writeoff_mat_loss = writeoff_lf * per_lf_rate  # reminder: included above, shown separately

    mat_total = mat_per_lf_cost + formula_mat_cost
    with c2c:
        st.metric("Per-LF materials",    f"${mat_per_lf_cost:,.2f} / mo")
        st.metric("Formula materials",   f"${formula_mat_cost:,.2f} / mo")
        st.metric("(Of which write-off loss)", f"${writeoff_mat_loss:,.2f}", help="Embedded in per-LF cost above — shown for visibility.")

    costs["2. Raw & Process Materials"] = mat_total
    st.markdown(f'<div class="subtotal">Section 2 Subtotal — Materials: ${mat_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — PACKAGING & TRANSFER MATERIALS
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 3 — PACKAGING & TRANSFER MATERIALS", expanded=False):
    c3a, c3b, c3c = st.columns([1, 1, 1])
    with c3a:
        pkg_cost_lf         = st.number_input("Packaging — corner guards, wrap, straps ($ / LF shipped)",      min_value=0.0, value=0.04, step=0.005, format="%.3f")
        label_cost_per_run  = st.number_input("Bundle labels & barcodes per production run ($)",                min_value=0.0, value=3.0,  step=0.50)
        van_lining_per_run  = st.number_input("Van lining / protective materials per run ($)",                  min_value=0.0, value=5.0,  step=0.50)
    with c3b:
        rework_pkg_lf       = st.number_input("Re-packaging for rework returns ($ / LF re-packaged)",          min_value=0.0, value=0.02, step=0.005, format="%.3f")

    pkg_total = (
        pkg_cost_lf * monthly_lf
        + label_cost_per_run * runs_per_month
        + van_lining_per_run * runs_per_month
        + rework_pkg_lf * rework_lf
    )
    with c3c:
        st.metric("Packaging & transfer", f"${pkg_total:,.2f} / mo")

    costs["3. Packaging & Transfer"] = pkg_total
    st.markdown(f'<div class="subtotal">Section 3 Subtotal — Packaging: ${pkg_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — TRANSPORTATION
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 4 — TRANSPORTATION  (Van Nuys ↔ Gardena)", expanded=False):
    st.markdown('<div class="sec-header">4.1  Variable Per-Run Costs</div>', unsafe_allow_html=True)
    c4a, c4b, c4c = st.columns([1, 1, 1])
    with c4a:
        distance_one_way    = st.number_input("Distance one-way Van Nuys → Gardena (miles)", min_value=0.0, value=17.0, step=0.5)
        mpg                 = st.number_input("Vehicle fuel efficiency (MPG)",                min_value=1.0, value=14.0, step=0.5)
        fuel_price          = st.number_input("Fuel price ($ / gallon)",                     min_value=0.0, value=4.80, step=0.05)
    with c4b:
        driver_transport_rate = st.number_input("Driver hourly rate — transport ($)",         min_value=0.0, value=20.0, step=0.50)
        drive_time_one_way    = st.number_input("Drive time one-way (hours)",                  min_value=0.0, value=0.75, step=0.25)
        rework_extra_runs     = st.number_input("Extra round-trips per month for rework returns", min_value=0.0, value=2.0, step=0.5)

    total_runs      = runs_per_month + rework_extra_runs
    miles_per_run   = distance_one_way * 2
    fuel_cost       = (miles_per_run * total_runs / mpg) * fuel_price
    driver_trans    = driver_transport_rate * (drive_time_one_way * 2) * total_runs

    with c4c:
        st.metric("Fuel cost",           f"${fuel_cost:,.2f} / mo",    help=f"{miles_per_run * total_runs:,.0f} total miles")
        st.metric("Driver transport labor", f"${driver_trans:,.2f} / mo")

    st.markdown('<div class="sec-header">4.2  Fixed Vehicle Ownership (pro-rated monthly)</div>', unsafe_allow_html=True)
    c4a, c4b, c4c = st.columns([1, 1, 1])
    with c4a:
        veh_insurance    = st.number_input("Vehicle insurance ($ / month)",              min_value=0.0, value=180.0, step=5.0)
        veh_depreciation = st.number_input("Vehicle depreciation ($ / month)",           min_value=0.0, value=250.0, step=10.0)
    with c4b:
        veh_maintenance  = st.number_input("Vehicle maintenance reserve ($ / month)",    min_value=0.0, value=120.0, step=10.0)
        veh_registration = st.number_input("Registration & licensing ($ / month)",       min_value=0.0, value=25.0,  step=5.0)
    veh_fixed = veh_insurance + veh_depreciation + veh_maintenance + veh_registration
    with c4c:
        st.metric("Vehicle fixed costs", f"${veh_fixed:,.2f} / mo")

    transport_total = fuel_cost + driver_trans + veh_fixed
    costs["4. Transportation"] = transport_total
    st.markdown(f'<div class="subtotal">Section 4 Subtotal — Transportation: ${transport_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — FACILITY: GARDENA PRODUCTION SPACE
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 5 — FACILITY: GARDENA PRODUCTION SPACE", expanded=False):
    c5a, c5b, c5c = st.columns([1, 1, 1])
    with c5a:
        g_rent         = st.number_input("Monthly rent — Gardena production space ($)",           min_value=0.0, value=3_500.0, step=50.0)
        g_elec_mach    = st.number_input("Electricity — machinery: sprayers, sanders, compressor ($)", min_value=0.0, value=400.0,   step=10.0)
        g_elec_light   = st.number_input("Electricity — lighting & HVAC / ventilation ($)",       min_value=0.0, value=150.0,   step=10.0)
    with c5b:
        g_gas          = st.number_input("Gas / propane — heated drying room ($)",                min_value=0.0, value=80.0,    step=5.0)
        g_maintenance  = st.number_input("Facility maintenance reserve ($/month)",                min_value=0.0, value=100.0,   step=10.0)
        g_waste        = st.number_input("Waste disposal — solvent-contaminated materials ($)",   min_value=0.0, value=120.0,   step=10.0)
    gardena_total = g_rent + g_elec_mach + g_elec_light + g_gas + g_maintenance + g_waste
    with c5c:
        st.metric("Gardena facility", f"${gardena_total:,.2f} / mo")

    costs["5. Facility — Gardena"] = gardena_total
    st.markdown(f'<div class="subtotal">Section 5 Subtotal — Gardena Facility: ${gardena_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — FACILITY: VAN NUYS SPACE (MOULDING ALLOCATED)
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 6 — FACILITY: VAN NUYS SPACE  (moulding-allocated sq ft only)", expanded=False):
    c6a, c6b, c6c = st.columns([1, 1, 1])
    with c6a:
        vn_cost_sqft      = st.number_input("Van Nuys facility cost ($ / sq ft / month)",          min_value=0.0, value=1.20,  step=0.05)
        raw_storage_sqft  = st.number_input("Raw moulding inventory storage (sq ft)",              min_value=0.0, value=300.0, step=10.0)
        drying_sqft       = st.number_input("Drying / acclimation staging area (sq ft)",           min_value=0.0, value=200.0, step=10.0)
    with c6b:
        finished_sqft     = st.number_input("Finished moulding warehouse storage (sq ft)",         min_value=0.0, value=400.0, step=10.0)
        qc_area_sqft      = st.number_input("QC inspection area (sq ft)",                          min_value=0.0, value=100.0, step=10.0)
    total_vn_sqft   = raw_storage_sqft + drying_sqft + finished_sqft + qc_area_sqft
    vn_facility_total = total_vn_sqft * vn_cost_sqft
    with c6c:
        st.metric("Moulding sq ft at Van Nuys", f"{total_vn_sqft:,.0f} sq ft")
        st.metric("Van Nuys allocated space",   f"${vn_facility_total:,.2f} / mo")

    costs["6. Facility — Van Nuys"] = vn_facility_total
    st.markdown(f'<div class="subtotal">Section 6 Subtotal — Van Nuys Space: ${vn_facility_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — MACHINERY & EQUIPMENT
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 7 — MACHINERY & EQUIPMENT", expanded=False):
    c7a, c7b, c7c = st.columns([1, 1, 1])
    with c7a:
        equip_depreciation = st.number_input("Equipment depreciation — all production machinery ($)", min_value=0.0, value=350.0, step=10.0)
        equip_maintenance  = st.number_input("Equipment maintenance reserve ($)",                      min_value=0.0, value=150.0, step=10.0)
    with c7b:
        spray_tips         = st.number_input("Consumable tooling — tips, nozzles, hoses ($)",          min_value=0.0, value=60.0, step=5.0)
        booth_filters      = st.number_input("Spray booth filter replacement ($)",                     min_value=0.0, value=45.0, step=5.0)
    equip_total = equip_depreciation + equip_maintenance + spray_tips + booth_filters
    with c7c:
        st.metric("Machinery & equipment", f"${equip_total:,.2f} / mo")

    costs["7. Machinery & Equipment"] = equip_total
    st.markdown(f'<div class="subtotal">Section 7 Subtotal — Machinery & Equipment: ${equip_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — QUALITY CONTROL & DEFECTS
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 8 — QUALITY CONTROL & DEFECTS", expanded=False):
    c8a, c8b, c8c = st.columns([1, 1, 1])
    with c8a:
        qc_supplies       = st.number_input("QC supplies — swatches, test materials ($)",                    min_value=0.0, value=50.0,  step=5.0)
        writeoff_handling = st.number_input("Write-off handling cost ($ / LF scrapped)",                     min_value=0.0, value=0.15,  step=0.01,
                                             help="Incremental labor to segregate, label, and dispose of fully rejected pieces.")
    with c8b:
        cust_return_pct   = st.number_input("Customer return rate (% of shipped LF)",                       min_value=0.0, max_value=10.0, value=1.0, step=0.1, format="%.1f") / 100.0
        cust_return_cost  = st.number_input("Replacement + re-delivery cost per returned LF ($)",            min_value=0.0, value=1.50,  step=0.05,
                                             help="Includes re-production, re-packaging, and re-delivery labor for field returns.")
        discount_cost     = st.number_input("Color-variance discount cost per month ($)",                    min_value=0.0, value=75.0,  step=5.0,
                                             help="Revenue given back when a batch is slightly off but customer accepts at a discount.")

    shipped_lf           = monthly_lf * (1.0 - writeoff_rate)
    writeoff_handle_cost = writeoff_lf * writeoff_handling
    cust_return_total    = shipped_lf * cust_return_pct * cust_return_cost

    qc_defect_total = qc_supplies + writeoff_handle_cost + cust_return_total + discount_cost
    with c8c:
        st.metric("QC supplies",           f"${qc_supplies:,.2f} / mo")
        st.metric("Write-off handling",    f"${writeoff_handle_cost:,.2f} / mo", help=f"{writeoff_lf:,.0f} LF scrapped")
        st.metric("Customer returns",      f"${cust_return_total:,.2f} / mo",    help=f"{shipped_lf * cust_return_pct:,.0f} LF returned")
        st.metric("Variance discounts",    f"${discount_cost:,.2f} / mo")

    costs["8. QC & Defects"] = qc_defect_total
    st.markdown(f'<div class="subtotal">Section 8 Subtotal — QC & Defects: ${qc_defect_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — COMPLIANCE & REGULATORY (California)
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 9 — COMPLIANCE & REGULATORY  (California / SCAQMD)", expanded=False):
    c9a, c9b, c9c = st.columns([1, 1, 1])
    with c9a:
        aqmd_permit  = st.number_input("SCAQMD VOC permit fees ($ / month)",                min_value=0.0, value=80.0,  step=5.0)
        haz_waste    = st.number_input("Hazardous waste disposal — solvents ($ / month)",   min_value=0.0, value=150.0, step=10.0)
        ppe_safety   = st.number_input("PPE & safety supplies ($ / month)",                 min_value=0.0, value=60.0,  step=5.0)
    with c9b:
        wc_uplift    = st.number_input("Workers' comp uplift — production classification ($)",  min_value=0.0, value=200.0, step=10.0,
                                        help="Delta above standard warehouse WC rate for staining/painting classification.")
        fire_code    = st.number_input("Fire code compliance — inspections, equipment ($)",  min_value=0.0, value=30.0,  step=5.0)
        comp_hours   = st.number_input("Compliance reporting labor (hours / month)",         min_value=0.0, value=3.0,   step=0.5)
        comp_rate    = st.number_input("Compliance admin hourly rate ($)",                   min_value=0.0, value=25.0,  step=0.5)
    compliance_total = aqmd_permit + haz_waste + ppe_safety + wc_uplift + fire_code + (comp_hours * comp_rate)
    with c9c:
        st.metric("Compliance & regulatory", f"${compliance_total:,.2f} / mo")

    costs["9. Compliance & Regulatory"] = compliance_total
    st.markdown(f'<div class="subtotal">Section 9 Subtotal — Compliance: ${compliance_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — INVENTORY CARRYING COSTS
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 10 — INVENTORY CARRYING COSTS", expanded=False):
    c10a, c10b, c10c = st.columns([1, 1, 1])
    with c10a:
        raw_inv_val      = st.number_input("Average raw moulding inventory value ($)",         min_value=0.0, value=8_000.0,  step=500.0)
        wip_val          = st.number_input("Average WIP value — in transit / in production ($)", min_value=0.0, value=3_000.0, step=250.0)
        finished_inv_val = st.number_input("Average finished moulding inventory value ($)",    min_value=0.0, value=12_000.0, step=500.0)
    with c10b:
        cost_of_capital  = st.number_input("Cost of capital (annual %)",                       min_value=0.0, max_value=50.0, value=8.0,  step=0.5) / 100.0
        shrinkage_ann    = st.number_input("Inventory shrinkage & damage rate (annual %)",      min_value=0.0, max_value=20.0, value=2.0,  step=0.1) / 100.0
        obsolescence_ann = st.number_input("Obsolescence write-down rate — finished goods (annual %)", min_value=0.0, max_value=20.0, value=3.0, step=0.1) / 100.0
    total_inv          = raw_inv_val + wip_val + finished_inv_val
    monthly_capital    = total_inv       * cost_of_capital  / 12
    monthly_shrinkage  = total_inv       * shrinkage_ann    / 12
    monthly_obsolete   = finished_inv_val * obsolescence_ann / 12
    inv_total          = monthly_capital + monthly_shrinkage + monthly_obsolete
    with c10c:
        st.metric("Total inventory",       f"${total_inv:,.0f}")
        st.metric("Capital cost / mo",     f"${monthly_capital:,.2f}")
        st.metric("Shrinkage / mo",        f"${monthly_shrinkage:,.2f}")
        st.metric("Obsolescence / mo",     f"${monthly_obsolete:,.2f}")

    costs["10. Inventory Carrying"] = inv_total
    st.markdown(f'<div class="subtotal">Section 10 Subtotal — Inventory Carrying: ${inv_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — INDIRECT & ADMINISTRATIVE
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 11 — INDIRECT & ADMINISTRATIVE", expanded=False):
    c11a, c11b, c11c = st.columns([1, 1, 1])
    with c11a:
        purchasing_hrs   = st.number_input("Purchasing / procurement hours per month",    min_value=0.0, value=8.0,  step=0.5)
        purchasing_rate  = st.number_input("Purchasing hourly rate ($)",                   min_value=0.0, value=24.0, step=0.5)
        order_mgmt_hrs   = st.number_input("Order management hours per month",            min_value=0.0, value=10.0, step=0.5)
        order_mgmt_rate  = st.number_input("Order management hourly rate ($)",            min_value=0.0, value=22.0, step=0.5)
    with c11b:
        mgmt_hrs         = st.number_input("Management oversight hours per month",        min_value=0.0, value=15.0, step=0.5)
        mgmt_rate        = st.number_input("Management hourly rate ($)",                  min_value=0.0, value=45.0, step=1.0)
        inv_mgmt_hrs     = st.number_input("Inventory management hours per month",        min_value=0.0, value=6.0,  step=0.5)
        inv_mgmt_rate    = st.number_input("Inventory management hourly rate ($)",        min_value=0.0, value=22.0, step=0.5)
        software_alloc   = st.number_input("ERP / software allocation ($ / month)",       min_value=0.0, value=75.0, step=5.0)
    admin_total = (
        purchasing_hrs * purchasing_rate
        + order_mgmt_hrs * order_mgmt_rate
        + mgmt_hrs * mgmt_rate
        + inv_mgmt_hrs * inv_mgmt_rate
        + software_alloc
    )
    with c11c:
        st.metric("Admin & indirect", f"${admin_total:,.2f} / mo")

    costs["11. Admin & Indirect"] = admin_total
    st.markdown(f'<div class="subtotal">Section 11 Subtotal — Admin & Indirect: ${admin_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — SALES & CUSTOMER RELATIONSHIP
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 12 — SALES & CUSTOMER RELATIONSHIP", expanded=False):
    c12a, c12b, c12c = st.columns([1, 1, 1])
    with c12a:
        sample_cost      = st.number_input("Sample production cost ($ / month)",                       min_value=0.0, value=150.0, step=10.0)
        sales_hrs        = st.number_input("Custom moulding quoting / sales labor (hours / month)",    min_value=0.0, value=8.0,   step=0.5)
        sales_rate       = st.number_input("Sales staff hourly rate ($)",                               min_value=0.0, value=25.0,  step=0.5)
    with c12b:
        cs_hrs           = st.number_input("Customer service for moulding issues (hours / month)",     min_value=0.0, value=6.0,   step=0.5)
        cs_rate          = st.number_input("Customer service hourly rate ($)",                         min_value=0.0, value=20.0,  step=0.5)
        rush_premium     = st.number_input("Rush order premiums — overtime, expediting ($ / month)",   min_value=0.0, value=100.0, step=10.0,
                                            help="Extra cost when customers pull a rush that disrupts normal production scheduling.")
        eta_miss_cost    = st.number_input("ETA miss cost — estimated lost margin per month ($)",      min_value=0.0, value=200.0, step=25.0,
                                            help="Conservative estimate of margin lost to late deliveries: partial credits, lost reorders, etc.")
    sales_total = sample_cost + (sales_hrs * sales_rate) + (cs_hrs * cs_rate) + rush_premium + eta_miss_cost
    with c12c:
        st.metric("Sales & customer costs", f"${sales_total:,.2f} / mo")

    costs["12. Sales & Customer Relations"] = sales_total
    st.markdown(f'<div class="subtotal">Section 12 Subtotal — Sales & Customer Relations: ${sales_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — OPPORTUNITY COSTS
# ═══════════════════════════════════════════════════════════════════════════════
with st.expander("SECTION 13 — OPPORTUNITY COSTS", expanded=False):
    st.info(
        "**Opportunity costs are real economic costs even though they never appear in your P&L.** "
        "They represent value foregone by committing resources to moulding rather than the next-best alternative. "
        "Include them when evaluating whether the moulding department pays its own way."
    )
    c13a, c13b, c13c = st.columns([1, 1, 1])
    with c13a:
        space_opp   = st.number_input("Gardena space opportunity cost — sub-lease / alt-use value ($ / mo)",  min_value=0.0, value=1_000.0, step=50.0)
        vn_space_opp= st.number_input("Van Nuys moulding space opportunity cost ($ / mo)",                    min_value=0.0, value=500.0,   step=25.0)
    with c13b:
        mgmt_opp    = st.number_input("Management time opportunity cost ($ / mo)",                            min_value=0.0, value=500.0,   step=50.0,
                                       help="Value of owner/manager hours if redirected to core flooring growth.")
        van_opp     = st.number_input("Van / truck opportunity cost — other deliveries displaced ($ / mo)",   min_value=0.0, value=200.0,   step=25.0)
        complexity  = st.number_input("Complexity cost — broader SKU range, safety-stock premium ($ / mo)",   min_value=0.0, value=150.0,   step=25.0)
    opportunity_total = space_opp + vn_space_opp + mgmt_opp + van_opp + complexity
    with c13c:
        st.metric("Opportunity costs", f"${opportunity_total:,.2f} / mo")

    costs["13. Opportunity Costs"] = opportunity_total
    st.markdown(f'<div class="subtotal">Section 13 Subtotal — Opportunity Costs: ${opportunity_total:,.2f} / month</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# RESULTS DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════
st.divider()
st.markdown("## Results — True Cost Summary")

total_monthly   = sum(costs.values())
cost_per_lf     = total_monthly / monthly_lf if monthly_lf > 0 else 0.0
cost_per_lf_ex  = (total_monthly - opportunity_total) / monthly_lf if monthly_lf > 0 else 0.0  # ex-opportunity

# ── Key Metrics Row ────────────────────────────────────────────────────────────
km1, km2, km3, km4 = st.columns(4)
with km1:
    st.metric("Total Monthly Cost", f"${total_monthly:,.2f}")
with km2:
    st.metric("True Cost / Linear Foot (all-in)", f"${cost_per_lf:.4f}")
with km3:
    st.metric("True Cost / LF (excl. opportunity)", f"${cost_per_lf_ex:.4f}",
              help="Cost per LF without the opportunity-cost section, for P&L comparison.")
with km4:
    st.metric("Monthly Production", f"{monthly_lf:,.0f} LF",
              help=f"Gross processed: {gross_lf:,.0f} LF  |  Rework: {rework_lf:,.0f} LF  |  Write-off: {writeoff_lf:,.0f} LF")

st.divider()

# ── Make vs. Buy Comparison ────────────────────────────────────────────────────
st.markdown("### Make vs. Buy Comparison")
mvb1, mvb2, mvb3 = st.columns(3)
with mvb1:
    purchased_price = st.number_input(
        "Purchased finished moulding — market price ($ / LF)",
        min_value=0.0, value=1.80, step=0.05,
        help="What would you pay an outside vendor for finished, color-matched moulding?",
    )
with mvb2:
    delta_allin    = cost_per_lf - purchased_price
    delta_exopp    = cost_per_lf_ex - purchased_price
    st.metric("All-in premium over purchased",
              f"${abs(delta_allin):.4f} / LF {'MORE' if delta_allin > 0 else 'LESS'} than buying",
              delta=f"${delta_allin:+.4f}", delta_color="inverse")
with mvb3:
    monthly_cost_delta = delta_allin * monthly_lf
    st.metric("Monthly cost delta (all-in)",
              f"${abs(monthly_cost_delta):,.2f} / mo {'over' if monthly_cost_delta > 0 else 'under'} purchased",
              delta=f"${monthly_cost_delta:+,.2f}", delta_color="inverse")

st.divider()

# ── Break-Even & Margin Analysis ──────────────────────────────────────────────
st.markdown("### Selling Price & Margin Analysis")
be1, be2, be3, be4 = st.columns(4)
with be1:
    target_margin_pct = st.number_input("Target gross margin (%)", min_value=0.0, max_value=100.0, value=35.0, step=1.0)
    target_margin = target_margin_pct / 100.0
with be2:
    required_price = cost_per_lf / (1.0 - target_margin) if target_margin < 1.0 else 0.0
    st.metric(f"Required sell price at {target_margin_pct:.0f}% GM", f"${required_price:.4f} / LF")
with be3:
    current_sell = st.number_input("Current average selling price ($ / LF)", min_value=0.0, value=2.50, step=0.05)
with be4:
    actual_margin_pct = (current_sell - cost_per_lf) / current_sell * 100.0 if current_sell > 0 else 0.0
    st.metric("Actual gross margin", f"{actual_margin_pct:.1f}%",
              delta=f"{actual_margin_pct - target_margin_pct:.1f} pp vs. target",
              delta_color="normal")

st.divider()

# ── Detailed Breakdown Table ───────────────────────────────────────────────────
st.markdown("### Cost Breakdown by Category")
rows = []
for cat, val in sorted(costs.items(), key=lambda x: -x[1]):
    rows.append({
        "Category": cat,
        "Monthly Cost ($)": val,
        "Cost / LF ($)": val / monthly_lf if monthly_lf > 0 else 0.0,
        "% of Total": val / total_monthly * 100.0 if total_monthly > 0 else 0.0,
    })
df = pd.DataFrame(rows)

# Totals row
df.loc[len(df)] = {
    "Category": "TOTAL",
    "Monthly Cost ($)": total_monthly,
    "Cost / LF ($)": cost_per_lf,
    "% of Total": 100.0,
}

st.dataframe(
    df.style
        .format({"Monthly Cost ($)": "${:,.2f}", "Cost / LF ($)": "${:.4f}", "% of Total": "{:.1f}%"})
        .apply(lambda r: ["font-weight: bold; background-color: #dbe7f5"] * len(r) if r["Category"] == "TOTAL" else [""] * len(r), axis=1),
    use_container_width=True,
    hide_index=True,
)

# ── Charts ─────────────────────────────────────────────────────────────────────
st.markdown("### Visual Breakdown")
ch1, ch2 = st.columns(2)

chart_df = df[df["Category"] != "TOTAL"].copy()

with ch1:
    fig_bar = go.Figure(go.Bar(
        x=chart_df["Monthly Cost ($)"],
        y=chart_df["Category"],
        orientation="h",
        marker_color="#2d5f8a",
        text=chart_df["Monthly Cost ($)"].map("${:,.0f}".format),
        textposition="outside",
    ))
    fig_bar.update_layout(
        title="Monthly Cost by Category",
        xaxis_title="$ / month",
        yaxis={"autorange": "reversed"},
        height=480,
        margin=dict(l=10, r=80, t=40, b=20),
    )
    st.plotly_chart(fig_bar, use_container_width=True)

with ch2:
    fig_pie = px.pie(
        chart_df,
        values="Monthly Cost ($)",
        names="Category",
        title="Cost Distribution",
        hole=0.38,
        color_discrete_sequence=px.colors.sequential.Blues_r,
    )
    fig_pie.update_traces(textposition="inside", textinfo="percent+label")
    fig_pie.update_layout(showlegend=False, height=480)
    st.plotly_chart(fig_pie, use_container_width=True)

# ── Waterfall ──────────────────────────────────────────────────────────────────
st.markdown("### Cost Build-Up  (Waterfall)")
sorted_cats = sorted(costs.items(), key=lambda x: x[0])
wf_x     = [c for c, _ in sorted_cats] + ["TOTAL"]
wf_y     = [v for _, v in sorted_cats] + [total_monthly]
wf_meas  = ["relative"] * len(sorted_cats) + ["total"]

fig_wf = go.Figure(go.Waterfall(
    name="Cost build-up",
    orientation="v",
    measure=wf_meas,
    x=wf_x,
    y=wf_y,
    texttemplate="$%{y:,.0f}",
    textposition="outside",
    connector={"line": {"color": "#8baabf"}},
    increasing={"marker": {"color": "#2d5f8a"}},
    totals={"marker": {"color": "#1b3a5c"}},
))
fig_wf.update_layout(
    title="Cumulative Cost Build-Up per Month",
    yaxis_title="$ / month",
    xaxis_tickangle=-45,
    height=420,
    margin=dict(l=10, r=10, t=40, b=100),
)
st.plotly_chart(fig_wf, use_container_width=True)

# ── Footer ─────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "All figures are monthly. "
    f"Model run date: 2026-06-16.  "
    "Placeholder defaults are illustrative — replace with actuals before using for decisions."
)
