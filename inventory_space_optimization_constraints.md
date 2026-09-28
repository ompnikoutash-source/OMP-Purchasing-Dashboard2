# Inventory and Warehouse Space Optimization

## Purpose

This is the living specification for a staged inventory, assortment, and warehouse-space optimization project.

The eventual model should help answer questions such as:

- Which products should remain stocked, and at which warehouse?
- How much inventory should each warehouse hold?
- Which inventory is economically justified by demand and contribution margin?
- Which discontinued or inactive material should be transferred, sold, returned, discounted, or written off?
- When is warehouse space more valuable than the inventory currently occupying it?
- Is additional warehouse capacity economically justified?

This file is intentionally a specification and decision register first. It is not yet the production optimizer. Unknown values must remain explicit rather than being silently guessed.

## Project Isolation Boundary

This project is isolated from the pre-existing workspace workflows. Existing scripts, SQL files, workbooks, dashboards, configuration files, scheduled tasks, and generated outputs are read-only inputs unless the user explicitly authorizes a change to a specific existing file.

Project work may add or modify only files created specifically for this inventory and warehouse-space optimization project. New code must use separate output paths, must not overwrite existing exports, and must not change shared configuration, dependencies, scheduled tasks, or production data connections. Any future need to integrate with an existing workflow must first be proposed and explicitly approved.

## How to Use This File

1. Work through the constraints in the order below.
2. For each constraint, record the business definition, source system or file, field names, owner, and confidence.
3. Do not implement a calculation until its units, scope, and null/exception behavior are agreed.
4. Keep raw source values separate from derived values.
5. Every derived output must carry enough metadata to explain the inputs and model version that produced it.
6. Use the checkpoint log at the end after each work session.

Status values:

- `OPEN`: definition or source still needs confirmation.
- `PARTIAL`: some fields or rules are known; important gaps remain.
- `CONFIRMED`: business meaning and source have been verified.
- `IMPLEMENTED`: coded and covered by a diagnostic or test.
- `BLOCKED`: cannot proceed without an external decision or data access.

## Current Repository Anchors

These are starting points only. They do not mean the eventual model must reuse every file.

- `gartman_connection.py`: existing Gartman ODBC connection through the `Gartman` DSN.
- `inventory_cost_margin_report.sql`: existing inventory availability, sales, landed-cost, margin, and turnover logic.
- `inventory_available_by_location_serial.sql`: candidate source for location and serial/tag inventory detail.
- `inventory_on_hand_simple.sql`: candidate source for simpler inventory totals.
- `core/inventory.py`: existing inventory position and replenishment simulation functions.
- `demand_health_scoring.py`: existing demand trend, materiality, overstock, and understock diagnostic concepts.
- `catalog_intelligence/active_items.py`: existing item-level dropped-status and retired-collection rules.
- `inventory_plan_all.xlsx`: user-identified most up-to-date forecast/planning output. It covers the actively purchased/planned subset rather than the full item catalog; workbook schema, as-of date, and field definitions still need to be cataloged read-only before ingestion.
- Existing forecast JSON and forecast reconciliation agents: candidate forecast history and forecast accuracy sources.

These anchors must be checked against live data before being treated as authoritative.

---

# Stage 0: Modeling Scope and Decisions

## C-001 Decision scope

- Status: `CONFIRMED`
- Constraint or decision: Decide whether the first version optimizes assortment, inventory targets, warehouse allocation, slotting, or a deliberately limited combination.
- Confirmed starting scope: all products across the eight included warehouses, with priority on items having a qualifying purchase receipt within the prior 24 months. The project must also analyze older discontinued/residual inventory as a separate space and disposition problem.
- Initial decision surfaces: assortment/inventory priority, warehouse allocation, and the threshold at which holding or occupying space is more expensive than disposal recovery.
- Deferred scope: detailed bin-level slotting until the economic model is trusted.
- Data/source needed: management decision recorded here.
- Validation: outputs must distinguish the active-priority population from older residual inventory and must not recommend actions outside the approved scope.

## C-002 Planning horizon

- Status: `PARTIAL`
- Constraint: Define the time horizon for decisions and evaluation.
- Confirmed population filter: purchase receipt within the prior 24 months for priority treatment; the cutoff must be calculated from the run as-of date.
- Candidate evaluation values: 12 months for annual economics; 365 days for inventory simulation; 24 months for receipt-priority diagnostics.
- Remaining decision: confirm historical sales window and forward forecast horizon.
- Data/source needed: business planning calendar, forecast horizon, and purchase receipt dates.
- Validation: every report must show the as-of date, receipt cutoff date, historical window, and forward horizon.

## C-003 Optimization grain

- Status: `OPEN`
- Constraint: Define the base decision key.
- Candidate key: item/SKU x warehouse x sales UOM.
- Possible additions: product family, collection, customer segment, lot/serial, or channel.
- Data/source needed: Gartman item and location structure plus forecast grain.
- Validation: no aggregation may combine items or locations with materially different demand, cost, or space behavior without an explicit rule.

## C-003A Full catalog versus planning population

- Status: `CONFIRMED`
- Constraint: Keep the full item catalog and the actively planned/purchased population as separate populations.
- Confirmed rule: the project covers all products with inventory or relevant catalog records, but `inventory_plan_all.xlsx` is the starting forecast source only for the smaller subset that the company actively forecasts/purchases.
- Required population labels: `forecasted_active`, `recent_receipt_priority`, `residual_inventory_review`, `discontinued_or_dropped`, `special_order_or_exception`, and `catalog_only_no_inventory`.
- Data/source needed: the forecast workbook, Gartman item master/status fields, purchase receipts, and current inventory.
- Validation: no full-catalog item may be silently treated as forecastable merely because it exists in the item master; no on-hand residual may be omitted merely because it is absent from the forecast workbook.

## C-004 Corporate and warehouse scope

- Status: `CONFIRMED`
- Constraint: Identify company, division, warehouse, staging, showroom, consignment, and excluded locations.
- Confirmed company scope: Gartman company 1 only.
- Confirmed included locations: 1, 3, 4, 5, 6, 8, 9, and 51. Locations 1, 3, 4, 5, 6, 8, and 9 are owned warehouses; location 51 is the auxiliary warehouse.
- Confirmed exclusions: every location not in the included list, all other company numbers such as company 2 and company 3, and location 90. Location 90 is reserved for missing or damaged inventory and is not treated as normal space-consuming warehouse inventory.
- Data/source needed: Gartman company and location fields; business interpretation recorded here.
- Validation: location totals reconcile to the agreed company-1 inventory report, with an explicit excluded-location report.

## C-005 Decision timing and refresh cadence

- Status: `PARTIAL`
- Constraint: Define how frequently recommendations may change and when recommendations become effective.
- Confirmed data cadence: the forecast workbook is updated every weekday before 9:00 AM, and the new forecasts are saved over the workbook approximately 90 minutes later.
- Implication: the workbook is a rolling snapshot, not a historical archive. Any project extract must copy the source into an isolated, dated project snapshot before analysis; it must never modify the source workbook.
- Candidate recommendation cadence: daily data refresh, weekly recommendation cycle, monthly policy review.
- Remaining decision: define how often this project should create diagnostics and draft recommendations.
- Data/source needed: purchasing and warehouse operating cadence.
- Validation: recommendations must be reproducible from a dated project snapshot, even though the source workbook is overwritten.

## C-006 Hard versus soft constraints

- Status: `OPEN`
- Constraint: Classify each rule as hard (must never be violated), soft (can be violated with penalty), or diagnostic-only.
- Examples: physical capacity is hard; desired service level may be soft; strategic assortment may be diagnostic or hard depending on management policy.
- Data/source needed: management decisions.
- Validation: model output must identify violated soft constraints and never hide infeasibility.

---

# Stage 1: Item Eligibility and Assortment Rules

## C-101 Item identity and UOM conversion

- Status: `PARTIAL`
- Constraint: Establish a stable SKU/item identifier and convert stocking UOM to sales UOM without double conversion.
- Existing candidate fields: `ITEMMAST.IMITEM`, `IMUM1`, `IMUM2`, and `IMFACT`.
- Existing note: current inventory SQL applies `IMFACT` when stocking and sales UOM differ, while receipt costs are documented as already being in sales UOM.
- Data/source needed: Gartman item master and confirmed UOM semantics.
- Validation: test known bundle, carton, SF, LF, and piece examples; reconcile converted quantities to Gartman screens.

## C-102 Active, dropped, discontinued, and retired status

- Status: `PARTIAL`
- Constraint: Define which statuses may be replenished, transferred, sold from existing stock, or excluded from new purchasing.
- Existing candidate rule: `ITEMMAST.IMDROP = 'D'` is treated as dropped by the existing catalog tooling; `catalog_intelligence/active_items.py` also contains business-confirmed retired collections.
- User-provided candidate rule: `IMDELT = 'A'` means active. This field name and code meaning must be verified against Gartman before implementation because the existing workspace uses `IMDROP` for a separate dropped-status rule.
- Confirmed priority rule: a purchase receipt within the prior 24 months identifies the main current/priority population, but it does not by itself override a discontinued or dropped status.
- Provisional good-item rule: an item with `IMDELT = 'A'` and a qualifying purchase receipt within the prior 24 months is treated as a good/current priority item, subject to exception reporting.
- Remaining decision: verify `IMDELT` and reconcile it with `IMDROP`; define how recent-receipt items with conflicting status values are treated, and how active items with no recent receipt are treated.
- Data/source needed: `IMDELT`, `IMDROP`, collection fields, item status fields, purchase receipt dates, and management overrides.
- Validation: produce separate lists for good/current priority items, older residual items, status conflicts, and items lacking the expected active tag.

## C-103 Residual discontinued inventory

- Status: `OPEN`
- Constraint: Define treatment for discontinued inventory already on hand.
- Confirmed business concern: older discontinued material is an operational space burden and must be evaluated against better-selling/higher-margin uses of that space.
- Candidate actions: sell normally, transfer to one location, allocate to known demand, markdown, return to vendor, liquidate, donate, scrap, or write off.
- Required economic question: determine the point at which expected future recovery plus any strategic value is less than the cost of continued holding, handling, and occupied capacity, including the opportunity cost of using that space for other inventory.
- Data/source needed: item status, age, quantity, cost, condition, expected recovery value, customer/project demand, and disposition cost.
- Validation: every discontinued quantity must receive exactly one disposition state; no residual stock may disappear from the accounting reconciliation.

## C-104 Strategic and mandatory assortment

- Status: `OPEN`
- Constraint: Identify items that must remain available despite weak calculated economics.
- Examples: contractual commitments, key account requirements, collection completeness, substitutes for critical items, warranty/service obligations, or showroom requirements.
- Data/source needed: customer, sales, product-management, and contract records.
- Validation: model must distinguish economically selected items from policy-forced items.

## C-105 Substitution and complementarity

- Status: `OPEN`
- Constraint: Define products that substitute for or require one another.
- Data/source needed: product family, collection, color, size, compatible accessory, and sales/customer knowledge.
- Validation: test scenarios where reducing one SKU shifts demand to another instead of assuming demand is independent.

## C-106 New-item and cold-start treatment

- Status: `PARTIAL`
- Constraint: Define launch, ramp, and minimum-history rules for new items.
- Existing candidate: `demand_health_scoring.py` excludes items with insufficient active months from peer scoring.
- Data/source needed: item introduction date, launch plan, analogous items, and initial forecast.
- Validation: new items must not be classified as slow solely because they lack history.

---

# Stage 2: Demand and Forecast Constraints

## C-201 Historical demand definition

- Status: `PARTIAL`
- Constraint: Define demand as shipped sales, ordered demand, invoiced demand, allocated demand, or a combination.
- Current source map: the supplied inventory/margin query aggregates shipped sales from `GSFL2K.SHHEAD` and `GSFL2K.SHLINE`, using `SHIDAT`, `SLITEM`, `SLENET`, `SLBLUO`, and a company/location/invoice/order composite key.
- Current windows available: 30, 90, 180, and 365 days, plus first/last order dates and order counts.
- Remaining decision: determine whether the optimization baseline uses shipped demand only or adds open orders, backorders, and stockout-censored demand.
- Data/source needed: Gartman sales/order tables and existing forecasting extraction.
- Validation: reconcile a sample of item-month totals to a trusted sales report.

## C-202 Returns, credits, cancellations, and transfers

- Status: `OPEN`
- Constraint: Define whether returns and non-customer movements reduce demand, increase available stock, or are tracked separately.
- Data/source needed: transaction type and order/shipment fields.
- Validation: trace several known returns, transfers, and cancellations end to end.

## C-203 Stockout-censored demand

- Status: `OPEN`
- Constraint: Distinguish low observed sales caused by low demand from low sales caused by unavailable inventory.
- Data/source needed: daily or monthly available quantity, stockout days, backorders, lost-sales estimate, and open demand.
- Validation: compare model demand with observed sales for known stockout periods.

## C-204 Forecast source and version

- Status: `PARTIAL`
- Constraint: Define which forecast output is authoritative for each product family and how forecast versions are selected.
- Confirmed starting source: `inventory_plan_all.xlsx` in the project workspace, identified by the user as the most up-to-date forecasting data.
- Confirmed scope interpretation: this workbook is expected to cover the actively purchased/planned SKU subset, not the full item catalog. Absence from the workbook is therefore a meaningful population label and must not be treated as zero demand without checking sales and inventory history.
- Confirmed workbook sheets: `Inventory_Metrics`, `Inventory _Summary`, and `Monthly_Projections`.
- Confirmed `Inventory_Metrics` columns: `sku`, `description`, `vendor_number`, `vendor_name`, `collection`, `available_sf`, `on_po_sf`, `backorder_sf`, `inventory_position`, `lead_time_days`, `lead_time_source`, `demand_class`, `adi`, `cv2`, `forecast_method`, `forecast_wmape`, `forecast_wape`, `forecast_mase`, `forecast_rmsse`, `forecast_bias_pct`, `forecast_uf_share`, `forecast_composite_score`, `weekly_fc_mean`, `first_reorder_month`, `avg_monthly_demand`, `avg_weekly_demand`, `daily_mean_demand`, `weekly_std_demand`, `lead_time_weeks`, `lead_time_mean_demand`, `sd_demand_over_lt`, `sd_demand_monthly`, `safety_stock`, `reorder_point`, `reorder_quantity`, `sku_abc`, `ss_base`, `ss_lumpy`, `ss_uplift_raw`, `ss_uplift_applied_pre_budget`, `order_up_to_level`, `sf_per_pallet`, `pallets_per_container`, and `ss_uplift_applied`.
- Confirmed `Monthly_Projections` columns: `Month`, `Historical Demand`, `Safety Stock`, `Beginning Inventory`, `Forecast`, `Order Quantity`, `Ending Inventory`, `SKU`, `vendor_number`, `collection`, `vendor_name`, `description`, `Expected Arrivals`, `PO Arrivals`, `Backorders`, `sf_per_pallet`, and `pallets_per_container`.
- Current workbook knowledge: no relevant columns have yet been confirmed for `Inventory _Summary`; its row grain and contents remain open.
- `Monthly_Projections` is the candidate time-series source for historical demand, forecast, safety stock, beginning inventory, order quantity, ending inventory, expected arrivals, PO arrivals, and backorders. Its warehouse grain must still be verified; absent a warehouse key, these projections should be treated as company-level.
- Important limitation: the confirmed `Inventory_Metrics` columns do not include a warehouse/location key. Do not interpret these values as warehouse-specific until verified; company-level forecast metrics may need to be allocated to warehouses using Gartman sales and inventory data.
- Required workbook metadata still open: row grain, inventory metric as-of date, forecast period, forecast snapshot/as-of date, whether `available_sf` and related quantities are company totals, and the role of each of the other two sheets.
- Data/source needed: `inventory_plan_all.xlsx`, forecast JSON/workbooks where needed, forecast method, snapshot date, target period, and reconciliation history.
- Validation: model input must identify forecast method and snapshot timestamp; workbook rows must reconcile to the actively planned SKU list; missing forecast rows must be reported separately.

## C-205 Forecast uncertainty

- Status: `OPEN`
- Constraint: Define forecast error or uncertainty by item, family, warehouse, and horizon.
- Candidate measures: historical residual standard deviation, forecast interval, quantile forecast, or empirical error distribution.
- Data/source needed: forecast history and actuals.
- Validation: back-test coverage of uncertainty intervals.

## C-206 Seasonality and business events

- Status: `OPEN`
- Constraint: Include seasonality, promotions, projects, construction cycles, and known customer events where material.
- Data/source needed: calendar, promotion history, project/customer commitments, and forecast adjustments.
- Validation: compare seasonal items against a non-seasonal baseline.

## C-207 Demand allocation across warehouses

- Status: `OPEN`
- Constraint: Decide whether demand is forecast at warehouse level or centrally and then allocated.
- Data/source needed: shipment origin, customer destination, transfer history, and warehouse service territory.
- Validation: location-level forecast totals must reconcile to the company-level forecast.

---

# Stage 3: Inventory State and Service Constraints

## C-301 Available inventory definition

- Status: `CONFIRMED`
- Constraint: Define available quantity by item and warehouse.
- Confirmed source logic from the supplied query: live `GSFL2K.ITEMBAL` with `IBCO = 1`, included locations only, and available quantity calculated as `IBQOH - IBQOO - COALESCE(IBQAL, 0)`.
- Confirmed UOM logic: when `IMFACT = 0` or `IMUM1 = IMUM2`, retain the inventory quantity; otherwise multiply by `IMFACT` to convert stocking UOM to sales UOM.
- Confirmed exclusions: location 90 and all non-company-1/non-included locations.
- Data/source needed: live inventory tables and item master conversion fields.
- Validation: reconcile item/location samples to Gartman availability screens and preserve a separate physical-on-hand measure for space analysis.

## C-302 On-hand, allocated, committed, backordered, and on-order states

- Status: `CONFIRMED`
- Constraint: Keep physical on-hand, available-to-promise, allocated, customer-committed, open purchase orders, and backorders as separate states.
- Confirmed fields from the supplied query: `IBQOH` physical on-hand, `IBQOO` open sales-order commitment, `IBQAL` allocated picks, and `IBQBO` backorders from `GSFL2K.ITEMBAL`.
- Confirmed open vendor PO source: `GSFL2K.POLINE`, using `PLQORD - COALESCE(PLQREC, 0)` and excluding deleted lines; do not use summed `IBQOOV` as the primary source because inter-branch transfers can inflate it.
- Existing candidate: `core/inventory.py` tracks on-hand, backorders, inbound, and inventory position.
- Data/source needed: inventory, sales order, and purchase order tables.
- Validation: inventory-position equation reconciles for every item/location, and vendor PO quantities reconcile separately from transfer quantities.

## C-303 Inventory age and lot/serial restrictions

- Status: `OPEN`
- Constraint: Determine whether inventory can be pooled across lots/tags and whether age, condition, shade, dye lot, serial, or quality restrictions limit substitution or transfer.
- Data/source needed: `ITEMDETL`, serial/tag records, dates, and quality status.
- Validation: model must not recommend combining non-interchangeable inventory.

## C-304 Safety stock policy

- Status: `PARTIAL`
- Constraint: Define whether current safety stock is accepted as an input, recalculated, or compared as a diagnostic.
- Candidate inputs: lead-time demand, forecast error, service target, review period, minimum order quantity, and demand class.
- Data/source needed: forecasting model outputs and policy parameters.
- Validation: independently recompute safety stock for a sample and explain differences.

## C-305 Service-level target

- Status: `OPEN`
- Constraint: Define desired fill rate or stockout probability by item class, customer, family, and warehouse.
- Data/source needed: customer commitments, sales policy, and management decisions.
- Validation: simulate policy and report achieved service level separately from target.

## C-306 Lead time and lead-time variability

- Status: `PARTIAL`
- Constraint: Define supplier-to-warehouse lead time, transfer lead time, receiving delay, and variability.
- Data/source needed: PO history, receipt dates, vendor, origin, and warehouse.
- Validation: compare stated lead times with empirical receipt intervals and flag stale estimates.

## C-307 Reorder, order-up-to, MOQ, and case-pack rules

- Status: `PARTIAL`
- Constraint: Define feasible replenishment quantities and timing.
- Candidate inputs: reorder point, order-up-to level, MOQ, case pack, pallet quantity, vendor calendar, and order frequency.
- Data/source needed: existing planning workbooks, PO history, vendor master, and purchasing policy.
- Validation: every recommended order must be expressible in valid supplier units and must respect timing.

---

# Stage 4: Space and Warehouse Constraints

## C-401 Physical dimensions and storage footprint

- Status: `PARTIAL`
- Constraint: Capture dimensions and the space actually consumed in each storage mode.
- Phase 1 decision: do not require dimensions for every SKU and do not equate raw units across unrelated products. Use product-family-level current quantities as a provisional capacity proxy.
- Required fields for the proxy: product family, item/SKU, current units in the agreed sales or stocking UOM, and warehouse/location where available.
- Proposed representation: `family_capacity_units`, an indexed capacity measure derived from the current baseline quantity by family. This is not literal square footage, cubic feet, or pallet positions.
- Candidate forecast fields: `sf_per_pallet` and `pallets_per_container` in `Inventory_Metrics` may help calibrate family burden factors, but their definitions, units, and calculation source must be verified first.
- Limitation: current family unit mix describes the present capacity allocation, not necessarily the economically optimal mix. The model must label it as a baseline allocation and test alternative mixes against that baseline.
- Upgrade path: introduce family-specific burden factors or storage-mode coefficients when warehouse experience, pallet counts, or representative measurements become available.
- Data/source needed: Gartman product family table, current item/location quantities, UOM definitions, and later family-level storage calibration.
- Validation: family quantities reconcile to the current inventory baseline; all recommendations report indexed capacity units and avoid claiming physical-space precision.

## C-401A Family capacity proxy and equivalent units

- Status: `PARTIAL`
- Constraint: Convert heterogeneous family quantities into a comparable planning resource without treating unlike physical units as interchangeable.
- Proposed formulation: `equivalent_capacity_units = current_or_planned_family_units * family_burden_factor`.
- Confirmed policy direction: allow family shares to change subject to one total indexed-capacity limit.
- Confirmed limitation: management cannot currently provide reliable cross-family conversion ratios, such as how much reduction in one family creates capacity for another family.
- Phase 1 treatment: do not claim that raw family units are physically interchangeable. Use the current inventory allocation to establish the total baseline capacity index, then run relative and bounded scenarios with burden factors explicitly marked as unknown or provisional.
- Preferred next option: estimate relative burden factors by family using representative pallet counts, storage-mode observations, container quantities, or informed management ratios. Until then, report which recommendations change when burden factors vary.
- Validation: report raw units, burden factor, equivalent capacity units, baseline family share, proposed family share, change from baseline, and sensitivity to burden-factor assumptions. A family-to-family trade must not be presented as operationally reliable unless its burden factors are supported.

## C-402 Storage mode and orientation

- Status: `PARTIAL`
- Constraint: Define whether the item is stored on pallet, rack, shelf, bin, floor stack, A-frame, showroom, yard, or another mode.
- Phase 1 approach: storage mode can remain a family-level or product-type-level attribute rather than an SKU-level dimension requirement.
- Initial facility input: warehouse dimensions alone are not sufficient; lanes, desks, bathrooms, breakrooms, hallways, docks, staging, and safety areas must be modeled as non-storage deductions or utilization factors. For the family proxy, these deductions are represented through the total baseline capacity index rather than estimated square footage.
- Data/source needed: warehouse operations and location master.
- Validation: later physical-space calculations must use mode-specific footprints, not generic cubic volume; phase 1 family-index outputs must be clearly labeled as proxies.

## C-403 Stackability and nesting

- Status: `OPEN`
- Constraint: Capture maximum stack height, nesting, crush limits, orientation, overhang, and safety clearance.
- Data/source needed: warehouse safety rules, packaging specs, and observed storage practice.
- Validation: calculated capacity must not exceed safe stack or rack limits.

## C-404 Fixed and variable capacity

- Status: `PARTIAL`
- Constraint: Separate total building capacity, usable capacity, reserved capacity, and capacity consumed by aisles, docks, staging, offices, and safety zones.
- Confirmed operating assumption: each included warehouse is currently at effective capacity with its present inventory amount.
- Initial capacity method: use a provisional location-level estimate supplied by the user, then reserve explicit deductions for forklift lanes, sales desks, bathrooms, breakrooms, hallways, docks/staging, and other non-storage areas.
- Important limitation: the first capacity values will be educated estimates, not exact usable-space measurements. They must be labeled provisional and should support scenario/sensitivity analysis rather than a false precision claim.
- Upgrade path: replace provisional estimates with better measurements or warehouse-layout estimates as they become available.
- Data/source needed: rough dimensions for each warehouse, operational space deductions, current inventory footprint, and later improved facility estimates.
- Validation: report gross estimated area, reserved/non-storage area, modeled usable storage area, current modeled occupied area, and residual capacity separately; compare the baseline against the confirmed "currently full" assumption.

## C-405 Warehouse-specific capacity

- Status: `PARTIAL`
- Constraint: Set capacity separately for each warehouse and storage mode.
- Confirmed baseline assumption: capacity is location-specific and fully occupied at the current inventory baseline.
- Initial source: rough dimensions and operational deductions provided by the user for locations 1, 3, 4, 5, 6, 8, 9, and 51.
- Data/source needed: facility measurements and location master.
- Validation: no recommendation may place inventory in a warehouse or mode without available capacity; baseline capacity should not imply unverified spare room.

## C-406 Pick-face and replenishment capacity

- Status: `OPEN`
- Constraint: Model forward-pick locations separately from reserve storage when relevant.
- Data/source needed: slotting layout, pick-face dimensions, replenishment labor, and pick frequency.
- Validation: high-velocity items must not be assigned more pick-face inventory than the location can hold.

## C-407 Handling and labor capacity

- Status: `OPEN`
- Constraint: Include receiving, putaway, picking, replenishment, transfers, loading, and disposition labor where material.
- Data/source needed: warehouse labor rates, time studies, wage data, and activity volumes.
- Validation: labor demand remains within available hours or is reported as a constrained resource.

## C-408 Transfer feasibility

- Status: `OPEN`
- Constraint: Define transfer cost, transfer lead time, minimum transfer quantity, and whether product can move between warehouses.
- Data/source needed: transfer history, freight rates, labor, and operating policy.
- Validation: recommendations must include transfer economics and arrival timing.

---

# Stage 5: Economic Constraints and Objective Function

## C-501 Selling price and net revenue

- Status: `PARTIAL`
- Constraint: Define realized selling price after discounts, rebates, credits, returns, and customer-specific pricing.
- Current source map: shipped net sales from `GSFL2K.SHLINE.SLENET`; list/preferred price from `ITEMMAST.IMP1`; the supplied query also retrieves product/item VIP pricing for price context.
- Remaining decision: confirm whether `SLENET` is the authoritative net revenue for contribution analysis and which discounts, rebates, freight, commissions, credits, and returns require separate treatment.
- Data/source needed: Gartman sales lines, pricing tables, and rebate/discount rules.
- Validation: reconcile net revenue for a sample item-period to financial reporting.

## C-502 Unit cost basis

- Status: `PARTIAL`
- Constraint: Define the cost used for decisions: latest landed cost, weighted inventory cost, standard cost, replacement cost, or another basis.
- Current source map: latest qualifying PO receipt from `GSFL2K.ITEMRECH` with company 1, `IRSRC = 'P'`, positive quantity/cost, and highest receipt number; weighted average live-tag cost from `GSFL2K.ITEMDETL` using `SUM(IDCOST * IDQOH) / SUM(IDQOH)`.
- Confirmed unit interpretation from the supplied query: `IRCOST` and `IDCOST` are treated as sales-UOM costs; do not apply `IMFACT` to those costs.
- Remaining decision: finance/purchasing must select the primary decision basis and whether freight factor `IRFFAC` is included separately.
- Data/source needed: accounting and purchasing confirmation.
- Validation: compare each candidate cost method and quantify recommendation sensitivity.

## C-503 Landed and variable cost components

- Status: `OPEN`
- Constraint: Identify freight, duty, brokerage, handling, inbound labor, damage, and other variable costs included in landed cost.
- Data/source needed: PO receipts, AP/freight records, vendor terms, and accounting policy.
- Validation: reconcile a set of receipts to the chosen landed-cost definition.

## C-504 Contribution margin definition

- Status: `OPEN`
- Constraint: Define whether contribution margin includes only product cost or also variable selling, handling, freight, commission, and payment costs.
- Proposed form: net revenue minus costs that change with the decision.
- Data/source needed: accounting and sales policy.
- Validation: finance sign-off on the contribution formula.

## C-505 Holding cost and cost of capital

- Status: `OPEN`
- Constraint: Assign an annual or period holding cost to inventory value, including capital, insurance, shrinkage, damage, taxes, and storage where appropriate.
- Data/source needed: finance, insurance, tax, shrinkage, and warehouse cost records.
- Validation: show each component separately and compare total to finance-approved carrying-cost assumptions.

## C-506 Obsolescence and markdown recovery value

- Status: `OPEN`
- Constraint: Estimate the recoverable value and cost of disposition for aging, discontinued, damaged, or excess stock.
- Data/source needed: markdown history, liquidation/return records, age buckets, and management estimates.
- Validation: back-test estimated recovery against historical dispositions.

## C-507 Warehouse fixed cost

- Status: `OPEN`
- Constraint: Identify fixed warehouse costs and decide whether they are included in optimization or shown only in capacity expansion analysis.
- Candidate components: rent, depreciation, utilities, insurance, management, equipment leases, maintenance, and property costs.
- Data/source needed: finance and facility records.
- Validation: fixed-cost total reconciles to the approved budget and is not incorrectly charged per unit.

## C-508 Incremental capacity cost

- Status: `OPEN`
- Constraint: Define the cost of adding capacity, outsourcing storage, leasing overflow, or expanding a facility.
- Data/source needed: facilities and finance estimates.
- Validation: compare the shadow value of capacity with the incremental cost under consistent time units.

## C-509 Cash and working-capital limit

- Status: `OPEN`
- Constraint: Define cash tied up in inventory and any purchasing or working-capital ceiling.
- Data/source needed: finance policy and inventory valuation.
- Validation: recommended inventory value and cash exposure remain within the approved limit.

## C-510 Objective function and penalties

- Status: `OPEN`
- Constraint: Agree on the quantity being maximized or minimized.
- Candidate objective: expected contribution margin minus holding, handling, transfer, stockout, markdown, disposal, and incremental capacity costs.
- Data/source needed: decisions from C-504 through C-509.
- Validation: produce a transparent objective bridge for every recommendation.

---

# Stage 6: Operational, Customer, and Policy Constraints

## C-601 Customer service and geographic coverage

- Status: `OPEN`
- Constraint: Define response-time, delivery-radius, and warehouse service-territory requirements.
- Data/source needed: customer locations, delivery routes, service promises, and transfer times.
- Validation: simulate service impact of moving or centralizing stock.

## C-602 Supplier and vendor constraints

- Status: `OPEN`
- Constraint: Capture vendor minimums, order calendars, production limits, allocation rules, and discontinuation notices.
- Data/source needed: vendor master, PO history, quotes, and buyer knowledge.
- Validation: recommended buys must be feasible under supplier rules.

## C-603 Product compatibility and quality constraints

- Status: `OPEN`
- Constraint: Respect shade, dye lot, grade, finish, size, batch, expiration, hazardous-material, and quality restrictions.
- Data/source needed: item and lot attributes plus warehouse policy.
- Validation: recommendations cannot pool or substitute incompatible stock.

## C-604 Internal transfer and ownership constraints

- Status: `OPEN`
- Constraint: Define whether inventory may cross divisions, companies, ownership classes, or customer reservations.
- Data/source needed: legal/entity and inventory ownership rules.
- Validation: reconciliation by entity and ownership class.

## C-605 Management overrides

- Status: `OPEN`
- Constraint: Provide explicit overrides for strategic items, promotions, known projects, and temporary exceptions.
- Data/source needed: controlled override table with owner, reason, start date, end date, and approval.
- Validation: every override is visible, dated, and included in model output.

---

# Stage 7: Data Quality, Reconciliation, and Diagnostics

## C-701 Snapshot consistency

- Status: `OPEN`
- Constraint: Ensure inventory, sales, cost, forecast, and dimensions refer to compatible as-of dates.
- Data/source needed: extraction timestamps and snapshot metadata.
- Validation: reject or flag mixed snapshots beyond the agreed tolerance.

## C-702 Missing and anomalous values

- Status: `OPEN`
- Constraint: Define handling for missing dimensions, zero/negative cost, negative inventory, unknown status, impossible UOM conversion, and duplicate records.
- Data/source needed: data profiling outputs.
- Validation: diagnostic report with counts, examples, and disposition for every anomaly class.

## C-703 Reconciliation totals

- Status: `OPEN`
- Constraint: Reconcile modeled totals to trusted Gartman and financial totals.
- Required reconciliations: units, inventory quantity, inventory value, sales revenue, cost, warehouse totals, and discontinued quantity.
- Validation: agreed tolerance by measure and an exception report outside tolerance.

## C-704 Explainability and audit trail

- Status: `OPEN`
- Constraint: Preserve source values, transformations, parameters, optimization status, shadow values, and recommendation reasons.
- Data/source needed: model output schema and run metadata.
- Validation: reproduce any recommendation from the saved run package.

## C-705 Scenario and sensitivity testing

- Status: `OPEN`
- Constraint: Test recommendations under changes to margin, demand, lead time, service level, capacity, and disposition recovery.
- Validation: report which decisions are robust and which depend on uncertain assumptions.

## C-706 Back-testing

- Status: `OPEN`
- Constraint: Compare the proposed policy with historical demand and actual replenishment outcomes without using future information.
- Data/source needed: dated forecast, inventory, PO, sales, and capacity snapshots.
- Validation: report fill rate, stockouts, inventory value, space, contribution, transfers, and disposal outcomes.

---

# Proposed Data Contracts

## Item master contract

Minimum candidate fields:

`item_id`, `description`, `family`, `collection`, `division`, `active_status`, `dropped_status`, `introduction_date`, `sales_uom`, `stocking_uom`, `uom_factor`, `vendor`, `dimensions`, `weight`, `storage_mode`, `strategic_flag`

## Location contract

Minimum candidate fields:

`warehouse_id`, `location_id`, `location_type`, `usable_capacity`, `capacity_unit`, `reserved_capacity`, `service_region`, `transfer_allowed`, `active_flag`

## Inventory snapshot contract

Minimum candidate fields:

`as_of_datetime`, `item_id`, `warehouse_id`, `location_id`, `lot_or_tag_id`, `on_hand`, `allocated`, `committed`, `available`, `backordered`, `on_order`, `unit_cost`, `inventory_value`, `age_date`, `condition`

## Demand and forecast contract

Minimum candidate fields:

`item_id`, `warehouse_id`, `period_start`, `period_end`, `observed_units`, `observed_revenue`, `orders`, `stockout_days`, `forecast_units`, `forecast_method`, `forecast_snapshot`, `forecast_error`, `lower_bound`, `upper_bound`

## Economics contract

Minimum candidate fields:

`item_id`, `warehouse_id`, `period`, `net_revenue`, `unit_cost`, `variable_cost`, `contribution_margin`, `holding_cost`, `handling_cost`, `transfer_cost`, `markdown_recovery`, `disposal_cost`

## Policy and override contract

Minimum candidate fields:

`item_id`, `warehouse_id`, `constraint_id`, `rule_type`, `value`, `unit`, `effective_from`, `effective_to`, `owner`, `approval`, `reason`, `source`

---

# Staged Build Plan

## Stage A: Register and source map

Deliverable: this file completed through C-101 to C-107, with exact source fields and first reconciliation queries.

No optimizer yet.

## Stage B: Read-only diagnostic extract

Deliverable: a dated, item/warehouse-level dataset combining Gartman inventory, sales, costs, status, dimensions, and existing forecast outputs.

Required outputs:

- source row counts
- missing-field report
- duplicate-key report
- unit and currency checks
- inventory and sales reconciliations
- discontinued inventory report

## Stage C: Baseline economics

Deliverable: transparent calculations for demand, turns, days of supply, contribution margin, holding cost, space use, and contribution per space unit.

## Stage D: Simulation and policy diagnostics

Deliverable: scenario engine that evaluates current policy and proposed policy without placing orders or changing live data.

## Stage E: Constrained optimization draft

Deliverable: draft recommendations with hard/soft constraint status, objective bridge, sensitivity analysis, and shadow values where supported.

## Stage F: Pilot and back-test

Deliverable: one warehouse or product family pilot with human review and historical back-test.

## Stage G: Production reporting

Deliverable: scheduled diagnostics, draft recommendations, parameter checks, run archive, and controlled promotion process.

---

# First Questions To Answer

Please answer these in order. Short answers are fine; unknown is a valid answer.

1. Which field in the Gartman product family table is the authoritative family code/key, and which field is the family description?
2. Can we verify that `IMDELT = 'A'` is the active code, and how does it relate to `IMDROP`?
3. Can you provide rough relative storage-burden categories for the major families, even without numeric ratios, such as small-box, pallet, long/rigid, flooring, moulding, or sundries?
4. Where should I find the authoritative dimensions and storage requirements for each product type if we later calibrate family burden factors?
5. Which fields identify special-order material beyond the active/discontinued status fields?
6. What business rules force an item to remain stocked despite weak sales or margin?
7. What is the trusted source for selling price, discounts, and net revenue?
8. Which cost basis should we compare first: latest landed cost, weighted inventory cost, standard cost, or replacement cost?
9. Who can verify fixed warehouse costs, variable handling costs, and incremental capacity costs?
10. How should a qualifying item with a recent receipt but a conflicting dropped/discontinued status be treated?
11. What is the preferred historical sales window and forward forecast horizon?

---

# Checkpoint Log

## Checkpoint 001 - Scope confirmed

- Date: 2026-09-04
- Completed: repository anchors identified; staged constraint register created; project isolation boundary recorded; company-1 and eight-location scope confirmed; all-product scope confirmed; 24-month purchase-receipt priority rule recorded; older residual inventory retained for disposal and space analysis; full-catalog versus forecast-subset rule recorded; supplied inventory/margin SQL mapped to source fields and calculation definitions; `inventory_plan_all.xlsx` confirmed as the starting forecast source; workbook sheets and columns recorded; `Inventory_Metrics` confirmed as one row per SKU; weekday-before-9am refresh and approximately 90-minute overwrite cadence recorded; current warehouse inventory treated as the full-capacity baseline; family shares allowed to change under one total indexed-capacity limit; lack of reliable cross-family conversion ratios recorded; provisional `IMDELT = 'A'` plus recent PO receipt rule recorded.
- Not yet completed: `IMDELT` meaning and relationship to `IMDROP` need verification; workbook as-of fields still need to be cataloged; `Inventory _Summary` contents remain unknown; `Monthly_Projections` warehouse grain needs confirmation; space-field units and family burden factors need confirmation; no economic assumptions approved; no production calculations added; no live data extracted.
- Next checkpoint: verify Gartman status fields and family key/description, then define qualitative or quantitative family burden factors and the first bounded capacity scenarios.
- Resume instruction: read this file from the first unanswered question, then update statuses and source fields before writing model code.
