/* ============================================================
   INVENTORY AVAILABILITY REPORT (NO COST DATA)
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Reports per item:
     - Current available inventory by location (sales UOM)
     - Sales $ and units over 30 / 90 / 180 / 365 days
     - Order counts over same windows
     - Last FOB Date                  (date of most recent PO receipt)
     - Avg Days Between POs           (avg interval between PO receipts, 5-year window)
     - Units sold for each of the last 12 completed calendar months
       (M-01 = most recent complete month, M-12 = oldest)
       Two trailer columns show the actual start dates so you can
       label the columns in Excel.

   NOTE: This is a cost-restricted version of the inventory/cost/margin
   report. It intentionally excludes Vendor Number/Name, both Landed
   Cost columns, both Gross Margin % columns, Begin Available (90D),
   and Inventory Turnover - Annualized — none of the underlying cost
   calculations (ITEMRECH.IRCOST, ITEMDETL.IDCOST, ITEMBA_EOD-based
   turnover) are computed in this query.

   INVENTORY AVAILABILITY NOTES:
     InvEnd / InvEndByLoc: live ITEMBAL (IBQOH - IBQOO - IBQAL).
       Matches the Gartman IMINQ01 screen (OE01014).
       IBQOO = open SO qty committed; IBQAL = allocated picks.
       Both reduce available inventory and must be subtracted.
       IMFACT converts stocking UOM (IMUM1) → sales UOM (IMUM2).
   ============================================================ */

WITH
Params AS (
    SELECT
        CURRENT_DATE AS TODAY,
        (CURRENT_DATE - 30 DAYS)  AS D30,
        (CURRENT_DATE - 90 DAYS)  AS D90,
        (CURRENT_DATE - 180 DAYS) AS D180,
        (CURRENT_DATE - 365 DAYS) AS D365
    FROM SYSIBM.SYSDUMMY1
),

-- First day of each of the last 12 completed calendar months.
-- M0 = first day of the current (incomplete) month.
-- M01_START = first day of the most recently completed month.
-- Refreshing on any date automatically shifts all 12 windows.
MonthBounds AS (
    SELECT
        M0,
        M0 -  1 MONTH  AS M01_START,
        M0 -  2 MONTHS AS M02_START,
        M0 -  3 MONTHS AS M03_START,
        M0 -  4 MONTHS AS M04_START,
        M0 -  5 MONTHS AS M05_START,
        M0 -  6 MONTHS AS M06_START,
        M0 -  7 MONTHS AS M07_START,
        M0 -  8 MONTHS AS M08_START,
        M0 -  9 MONTHS AS M09_START,
        M0 - 10 MONTHS AS M10_START,
        M0 - 11 MONTHS AS M11_START,
        M0 - 12 MONTHS AS M12_START
    FROM (
        SELECT CURRENT_DATE - (DAY(CURRENT_DATE) - 1) DAYS AS M0
        FROM SYSIBM.SYSDUMMY1
    ) X
),

OrderItem365 AS (
    SELECT
        L.SLITEM AS SLITEM,
        H.SHIDAT AS SHIDAT,
        (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#))) AS INVKEY,
        SUM(COALESCE(L.SLENET,0))  AS INV_ITEM_SALES,
        SUM(COALESCE(L.SLBLUO,0))  AS INV_ITEM_UNITS
    FROM Params P
    JOIN GSFL2K.SHHEAD H
      ON H.SHIDAT >= P.D365
    JOIN GSFL2K.SHLINE L
      ON L.SLCO   = H.SHCO
     AND L.SLLOC  = H.SHLOC
     AND L.SLINV# = H.SHINV#
     AND L.SLORD# = H.SHORD#
    GROUP BY
        L.SLITEM,
        H.SHIDAT,
        (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))
),

SalesAgg AS (
    SELECT
        SLITEM AS ITEM_NUMBER,

        MIN(SHIDAT) AS FIRST_ORDER_DATE_365,
        MAX(SHIDAT) AS LAST_ORDER_DATE_365,

        SUM(CASE WHEN SHIDAT >= (CURRENT_DATE - 30 DAYS)  THEN INV_ITEM_SALES ELSE 0 END) AS SALES_30D,
        SUM(CASE WHEN SHIDAT >= (CURRENT_DATE - 90 DAYS)  THEN INV_ITEM_SALES ELSE 0 END) AS SALES_90D,
        SUM(CASE WHEN SHIDAT >= (CURRENT_DATE - 180 DAYS) THEN INV_ITEM_SALES ELSE 0 END) AS SALES_180D,
        SUM(CASE WHEN SHIDAT >= (CURRENT_DATE - 365 DAYS) THEN INV_ITEM_SALES ELSE 0 END) AS SALES_365D,

        SUM(CASE WHEN SHIDAT >= (CURRENT_DATE - 30 DAYS)  THEN INV_ITEM_UNITS ELSE 0 END) AS UNITS_30D,
        SUM(CASE WHEN SHIDAT >= (CURRENT_DATE - 90 DAYS)  THEN INV_ITEM_UNITS ELSE 0 END) AS UNITS_90D,
        SUM(CASE WHEN SHIDAT >= (CURRENT_DATE - 180 DAYS) THEN INV_ITEM_UNITS ELSE 0 END) AS UNITS_180D,
        SUM(CASE WHEN SHIDAT >= (CURRENT_DATE - 365 DAYS) THEN INV_ITEM_UNITS ELSE 0 END) AS UNITS_365D,

        COUNT(DISTINCT CASE WHEN SHIDAT >= (CURRENT_DATE - 30 DAYS)  THEN INVKEY END) AS ORDERS_30D,
        COUNT(DISTINCT CASE WHEN SHIDAT >= (CURRENT_DATE - 90 DAYS)  THEN INVKEY END) AS ORDERS_90D,
        COUNT(DISTINCT CASE WHEN SHIDAT >= (CURRENT_DATE - 180 DAYS) THEN INVKEY END) AS ORDERS_180D,
        COUNT(DISTINCT CASE WHEN SHIDAT >= (CURRENT_DATE - 365 DAYS) THEN INVKEY END) AS ORDERS_365D,

        COUNT(DISTINCT INVKEY) AS ORDERS_LIFETIME_365SCAN
    FROM OrderItem365
    GROUP BY SLITEM
),

InvEnd AS (
    -- Live ITEMBAL: IBQOH=on-hand, IBQOO=open SO committed, IBQAL=allocated picks.
    -- Both IBQOO and IBQAL reduce available inventory (matches IMINQ01 screen).
    SELECT
        IB.IBITEM AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0))
                ELSE (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0)) * IM.IMFACT
            END
        ) AS END_AVAIL
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY IB.IBITEM
),

InvEndByLoc AS (
    SELECT
        X.ITEM_NUMBER,
        SUM(CASE WHEN X.IBLOC = 1  THEN X.AVAIL_SALES_UOM ELSE 0 END) AS LOC1_AVAIL,
        SUM(CASE WHEN X.IBLOC = 3  THEN X.AVAIL_SALES_UOM ELSE 0 END) AS LOC3_AVAIL,
        SUM(CASE WHEN X.IBLOC = 4  THEN X.AVAIL_SALES_UOM ELSE 0 END) AS LOC4_AVAIL,
        SUM(CASE WHEN X.IBLOC = 5  THEN X.AVAIL_SALES_UOM ELSE 0 END) AS LOC5_AVAIL,
        SUM(CASE WHEN X.IBLOC = 6  THEN X.AVAIL_SALES_UOM ELSE 0 END) AS LOC6_AVAIL,
        SUM(CASE WHEN X.IBLOC = 8  THEN X.AVAIL_SALES_UOM ELSE 0 END) AS LOC8_AVAIL,
        SUM(CASE WHEN X.IBLOC = 9  THEN X.AVAIL_SALES_UOM ELSE 0 END) AS LOC9_AVAIL,
        SUM(CASE WHEN X.IBLOC = 51 THEN X.AVAIL_SALES_UOM ELSE 0 END) AS LOC51_AVAIL
    FROM (
        SELECT
            IB.IBITEM AS ITEM_NUMBER,
            IB.IBLOC  AS IBLOC,
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0))
                ELSE (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0)) * IM.IMFACT
            END AS AVAIL_SALES_UOM
        FROM GSFL2K.ITEMBAL IB
        JOIN GSFL2K.ITEMMAST IM
          ON IM.IMITEM = IB.IBITEM
        WHERE IB.IBCO = 1
          AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    ) X
    GROUP BY X.ITEM_NUMBER
),

LatestPOReceipt AS (
    -- Date only — no cost fields. Used solely for "Last FOB Date".
    SELECT
        TRIM(R.IRITEM) AS ITEM_NUMBER,
        R.IRDATE       AS MOST_RECENT_RECEIVED_DATE
    FROM GSFL2K.ITEMRECH R
    WHERE R.IRCO = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRQTY > 0
      AND R.IRCOST > 0
      AND R.IRRECNBR > 0
      AND R.IRRECNBR = (
          SELECT MAX(R2.IRRECNBR)
          FROM GSFL2K.ITEMRECH R2
          WHERE TRIM(R2.IRITEM) = TRIM(R.IRITEM)
            AND R2.IRCO = 1
            AND TRIM(R2.IRSRC) = 'P'
            AND R2.IRQTY > 0
            AND R2.IRCOST > 0
            AND R2.IRRECNBR > 0
      )
),

AvgPOFrequency AS (
    -- Computes average days between PO receipts over the last 5 years.
    -- Uses distinct receipt dates (IRDATE) per item to avoid inflating
    -- the count when multiple lines share the same receipt date.
    -- Falls back to 365 when there is only one receipt event in the window
    -- (cannot compute an interval from a single data point).
    SELECT
        TRIM(IRITEM) AS ITEM_NUMBER,
        CASE
            WHEN COUNT(DISTINCT IRDATE) <= 1 THEN 365
            ELSE DECIMAL(
                     DAYS(MAX(IRDATE)) - DAYS(MIN(IRDATE)),
                     18, 2
                 ) / NULLIF(COUNT(DISTINCT IRDATE) - 1, 0)
        END AS AVG_DAYS_BETWEEN_POS
    FROM GSFL2K.ITEMRECH
    WHERE IRCO = 1
      AND TRIM(IRSRC) = 'P'
      AND IRQTY > 0
      AND IRCOST > 0
      AND IRRECNBR > 0
      AND IRDATE >= (CURRENT_DATE - 1825 DAYS)
    GROUP BY TRIM(IRITEM)
),

Backorders AS (
    -- Total quantity on backorder companywide, in sales UOM.
    -- IBQBO = qty ordered but unfulfillable due to insufficient stock.
    -- Same location scope and UOM conversion as InvEnd.
    SELECT
        IB.IBITEM AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN COALESCE(IB.IBQBO, 0)
                ELSE COALESCE(IB.IBQBO, 0) * IM.IMFACT
            END
        ) AS TOTAL_BACKORDER
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY IB.IBITEM
),

QtyOnPO AS (
    -- Open vendor PO quantity per item, in sales UOM.
    -- Sourced from POLINE instead of ITEMBAL.IBQOOV.
    -- IBQOOV is inflated when summed across locations because
    -- inter-branch transfer orders add to IBQOOV at the receiving
    -- location while the goods are already counted elsewhere.
    -- POLINE only contains true vendor purchase orders.
    -- PLQORD - PLQREC = remaining open qty per line (stocking UOM / IMUM1);
    -- IMFACT converts to sales UOM the same way availability columns do.
    SELECT
        TRIM(PL.PLITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN DECIMAL(PL.PLQORD - COALESCE(PL.PLQREC, 0), 18, 6)
                ELSE DECIMAL(PL.PLQORD - COALESCE(PL.PLQREC, 0), 18, 6) * IM.IMFACT
            END
        ) AS TOTAL_QTY_ON_PO
    FROM GSFL2K.POLINE PL
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = TRIM(PL.PLITEM)
    WHERE PL.PLCO = 1
      AND COALESCE(TRIM(PL.PLDELT), '') <> 'D'
      AND PL.PLQORD > COALESCE(PL.PLQREC, 0)
    GROUP BY TRIM(PL.PLITEM)
),

-- Units sold per item for each of the last 12 completed calendar months.
-- The date window is driven by MonthBounds so it shifts automatically on refresh.
-- SHIDAT range: [M12_START, M0) — covers all 12 months in a single table scan.
MonthlyUnits AS (
    SELECT
        L.SLITEM AS ITEM_NUMBER,
        SUM(CASE WHEN H.SHIDAT >= MB.M01_START AND H.SHIDAT < MB.M0        THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M01,
        SUM(CASE WHEN H.SHIDAT >= MB.M02_START AND H.SHIDAT < MB.M01_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M02,
        SUM(CASE WHEN H.SHIDAT >= MB.M03_START AND H.SHIDAT < MB.M02_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M03,
        SUM(CASE WHEN H.SHIDAT >= MB.M04_START AND H.SHIDAT < MB.M03_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M04,
        SUM(CASE WHEN H.SHIDAT >= MB.M05_START AND H.SHIDAT < MB.M04_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M05,
        SUM(CASE WHEN H.SHIDAT >= MB.M06_START AND H.SHIDAT < MB.M05_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M06,
        SUM(CASE WHEN H.SHIDAT >= MB.M07_START AND H.SHIDAT < MB.M06_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M07,
        SUM(CASE WHEN H.SHIDAT >= MB.M08_START AND H.SHIDAT < MB.M07_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M08,
        SUM(CASE WHEN H.SHIDAT >= MB.M09_START AND H.SHIDAT < MB.M08_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M09,
        SUM(CASE WHEN H.SHIDAT >= MB.M10_START AND H.SHIDAT < MB.M09_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M10,
        SUM(CASE WHEN H.SHIDAT >= MB.M11_START AND H.SHIDAT < MB.M10_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M11,
        SUM(CASE WHEN H.SHIDAT >= MB.M12_START AND H.SHIDAT < MB.M11_START THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_M12
    FROM MonthBounds MB
    JOIN GSFL2K.SHHEAD H
      ON H.SHIDAT >= MB.M12_START
     AND H.SHIDAT <  MB.M0
    JOIN GSFL2K.SHLINE L
      ON L.SLCO   = H.SHCO
     AND L.SLLOC  = H.SHLOC
     AND L.SLINV# = H.SHINV#
     AND L.SLORD# = H.SHORD#
    GROUP BY L.SLITEM
),

VipProductPrice AS (
    SELECT
        PP.PPPRCD AS PRICE_CODE,
        MAX(PP.PPP1) AS VIP_PRICE
    FROM GSFL2K.PRODPCOL PP
    WHERE TRIM(PP.PPPMCD) = 'WOOD-A'
    GROUP BY PP.PPPRCD
),

VipItemPrice AS (
    SELECT
        TRIM(IP.IPITEM) AS ITEM_NUMBER,
        MAX(IP.IPP1) AS VIP_PRICE
    FROM GSFL2K.ITEMPRIC IP
    WHERE TRIM(IP.IPPMCD) = 'WOOD-A'
    GROUP BY TRIM(IP.IPITEM)
)

SELECT
    TRIM(IM.IMITEM)    AS "Item Number",
    TRIM(IM.IMDESC)    AS "Description",
    TRIM(IX.IMCOLLECT) AS "Collection",
    IM.IMP1            AS "Preferred Price",
    COALESCE(VIP_ITEM.VIP_PRICE, VIP_PRODUCT.VIP_PRICE) AS "VIP Price",
    TRIM(IM.IMDIV)     AS "Division",

    DECIMAL(COALESCE(IEN.END_AVAIL, 0), 18, 2)         AS "Current Inventory Available",
    DECIMAL(COALESCE(BO.TOTAL_BACKORDER, 0), 18, 2)    AS "Backorders",
    DECIMAL(COALESCE(PO.TOTAL_QTY_ON_PO, 0), 18, 2)   AS "Qty on PO",
    TRIM(IM.IMUM2) AS "Sales Unit of Measurement",

    DECIMAL(COALESCE(LOC.LOC1_AVAIL,  0), 18, 2) AS "Loc 1 Avail",
    DECIMAL(COALESCE(LOC.LOC3_AVAIL,  0), 18, 2) AS "Loc 3 Avail",
    DECIMAL(COALESCE(LOC.LOC4_AVAIL,  0), 18, 2) AS "Loc 4 Avail",
    DECIMAL(COALESCE(LOC.LOC5_AVAIL,  0), 18, 2) AS "Loc 5 Avail",
    DECIMAL(COALESCE(LOC.LOC6_AVAIL,  0), 18, 2) AS "Loc 6 Avail",
    DECIMAL(COALESCE(LOC.LOC8_AVAIL,  0), 18, 2) AS "Loc 8 Avail",
    DECIMAL(COALESCE(LOC.LOC9_AVAIL,  0), 18, 2) AS "Loc 9 Avail",
    DECIMAL(COALESCE(LOC.LOC51_AVAIL, 0), 18, 2) AS "Loc 51 Avail",

    COALESCE(SA.SALES_30D,  0) AS "Sales $ - 30D",
    COALESCE(SA.SALES_90D,  0) AS "Sales $ - 90D",
    COALESCE(SA.SALES_180D, 0) AS "Sales $ - 180D",
    COALESCE(SA.SALES_365D, 0) AS "Sales $ - 365D",

    COALESCE(SA.ORDERS_30D,  0) AS "Orders - 30D",
    COALESCE(SA.ORDERS_90D,  0) AS "Orders - 90D",
    COALESCE(SA.ORDERS_180D, 0) AS "Orders - 180D",
    COALESCE(SA.ORDERS_365D, 0) AS "Orders - 365D",

    COALESCE(SA.UNITS_30D,  0) AS "Units Sold - 30D",
    COALESCE(SA.UNITS_90D,  0) AS "Units Sold - 90D",
    COALESCE(SA.UNITS_180D, 0) AS "Units Sold - 180D",
    COALESCE(SA.UNITS_365D, 0) AS "Units Sold - 365D",

    COALESCE(MU.UNITS_M01, 0) AS "Units M-01 (Most Recent Mo)",
    COALESCE(MU.UNITS_M02, 0) AS "Units M-02",
    COALESCE(MU.UNITS_M03, 0) AS "Units M-03",
    COALESCE(MU.UNITS_M04, 0) AS "Units M-04",
    COALESCE(MU.UNITS_M05, 0) AS "Units M-05",
    COALESCE(MU.UNITS_M06, 0) AS "Units M-06",
    COALESCE(MU.UNITS_M07, 0) AS "Units M-07",
    COALESCE(MU.UNITS_M08, 0) AS "Units M-08",
    COALESCE(MU.UNITS_M09, 0) AS "Units M-09",
    COALESCE(MU.UNITS_M10, 0) AS "Units M-10",
    COALESCE(MU.UNITS_M11, 0) AS "Units M-11",
    COALESCE(MU.UNITS_M12, 0) AS "Units M-12 (Oldest Mo)",

    MB.M01_START AS "M-01 Starts",
    MB.M12_START AS "M-12 Starts",

    LC.MOST_RECENT_RECEIVED_DATE                        AS "Last FOB Date",
    COALESCE(APF.AVG_DAYS_BETWEEN_POS, 365)             AS "Avg Days Between POs"

FROM GSFL2K.ITEMMAST IM
CROSS JOIN MonthBounds MB
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
LEFT JOIN SalesAgg SA
  ON SA.ITEM_NUMBER = IM.IMITEM
LEFT JOIN InvEnd IEN
  ON IEN.ITEM_NUMBER = IM.IMITEM
LEFT JOIN Backorders BO
  ON BO.ITEM_NUMBER = IM.IMITEM
LEFT JOIN QtyOnPO PO
  ON PO.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN InvEndByLoc LOC
  ON LOC.ITEM_NUMBER = IM.IMITEM
LEFT JOIN MonthlyUnits MU
  ON MU.ITEM_NUMBER = IM.IMITEM
LEFT JOIN VipProductPrice VIP_PRODUCT
  ON VIP_PRODUCT.PRICE_CODE = IM.IMPRCD
LEFT JOIN VipItemPrice VIP_ITEM
  ON VIP_ITEM.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN LatestPOReceipt LC
  ON TRIM(LC.ITEM_NUMBER) = TRIM(IM.IMITEM)
LEFT JOIN AvgPOFrequency APF
  ON TRIM(APF.ITEM_NUMBER) = TRIM(IM.IMITEM)

ORDER BY IM.IMITEM
