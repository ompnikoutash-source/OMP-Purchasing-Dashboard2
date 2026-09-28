/* ============================================================
   TOP SELLING ITEMS BY LOCATION — TRAILING 365 DAYS
   DB: DB2/AS400 via ODBC, schema GSFL2K

   "Trailing 365 days" = CURRENT_DATE - 365 DAYS through CURRENT_DATE - 1 DAY.
   Example: run on 2026-05-06  →  2025-05-06 through 2026-05-05

   Per-location columns for locations 1, 3, 4, 5, 6, 7, 8, 9:
     - Current Inventory Available (sales UOM, live from ITEMBAL)
     - Revenue Last 365 Days
     - Order Count Last 365 Days
     - Units Sold Last 365 Days

   Companywide availability includes loc 51 (warehouse/staging).
   Ordered by total revenue across all locations, descending.
   ============================================================ */

WITH
Params AS (
    SELECT
        CURRENT_DATE - 1 DAYS    AS PERIOD_END,    -- yesterday (e.g. 2026-05-05 when run on 2026-05-06)
        CURRENT_DATE - 365 DAYS  AS PERIOD_START   -- 365 days ago (e.g. 2025-05-06 when run on 2026-05-06)
    FROM SYSIBM.SYSDUMMY1
),

OrderItemByLoc AS (
    -- One row per item + location + invoice within the trailing-365-day window.
    -- Grouping at the invoice level lets COUNT(DISTINCT INVKEY) deduplicate orders.
    SELECT
        TRIM(L.SLITEM)                                                               AS ITEM_NUMBER,
        H.SHLOC                                                                      AS LOC,
        RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|'
            || RTRIM(CHAR(H.SHINV#))                                                 AS INVKEY,
        SUM(COALESCE(L.SLENET, 0))                                                   AS REV,
        SUM(COALESCE(L.SLBLUO, 0))                                                   AS UNITS
    FROM Params P
    JOIN GSFL2K.SHHEAD H
      ON  H.SHIDAT >= P.PERIOD_START
      AND H.SHIDAT <= P.PERIOD_END
      AND H.SHLOC  IN (1, 3, 4, 5, 6, 7, 8, 9)
    JOIN GSFL2K.SHLINE L
      ON  L.SLCO   = H.SHCO
      AND L.SLLOC  = H.SHLOC
      AND L.SLINV# = H.SHINV#
      AND L.SLORD# = H.SHORD#
    GROUP BY
        TRIM(L.SLITEM),
        H.SHLOC,
        RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#))
),

SalesAgg AS (
    SELECT
        ITEM_NUMBER,

        SUM(REV)               AS TOTAL_REV,
        COUNT(DISTINCT INVKEY) AS TOTAL_ORDERS,
        SUM(UNITS)             AS TOTAL_UNITS,

        SUM(CASE WHEN LOC = 1 THEN REV   ELSE 0 END)                            AS REV_LOC1,
        SUM(CASE WHEN LOC = 3 THEN REV   ELSE 0 END)                            AS REV_LOC3,
        SUM(CASE WHEN LOC = 4 THEN REV   ELSE 0 END)                            AS REV_LOC4,
        SUM(CASE WHEN LOC = 5 THEN REV   ELSE 0 END)                            AS REV_LOC5,
        SUM(CASE WHEN LOC = 6 THEN REV   ELSE 0 END)                            AS REV_LOC6,
        SUM(CASE WHEN LOC = 7 THEN REV   ELSE 0 END)                            AS REV_LOC7,
        SUM(CASE WHEN LOC = 8 THEN REV   ELSE 0 END)                            AS REV_LOC8,
        SUM(CASE WHEN LOC = 9 THEN REV   ELSE 0 END)                            AS REV_LOC9,

        COUNT(DISTINCT CASE WHEN LOC = 1 THEN INVKEY END)                       AS ORDERS_LOC1,
        COUNT(DISTINCT CASE WHEN LOC = 3 THEN INVKEY END)                       AS ORDERS_LOC3,
        COUNT(DISTINCT CASE WHEN LOC = 4 THEN INVKEY END)                       AS ORDERS_LOC4,
        COUNT(DISTINCT CASE WHEN LOC = 5 THEN INVKEY END)                       AS ORDERS_LOC5,
        COUNT(DISTINCT CASE WHEN LOC = 6 THEN INVKEY END)                       AS ORDERS_LOC6,
        COUNT(DISTINCT CASE WHEN LOC = 7 THEN INVKEY END)                       AS ORDERS_LOC7,
        COUNT(DISTINCT CASE WHEN LOC = 8 THEN INVKEY END)                       AS ORDERS_LOC8,
        COUNT(DISTINCT CASE WHEN LOC = 9 THEN INVKEY END)                       AS ORDERS_LOC9,

        SUM(CASE WHEN LOC = 1 THEN UNITS ELSE 0 END)                            AS UNITS_LOC1,
        SUM(CASE WHEN LOC = 3 THEN UNITS ELSE 0 END)                            AS UNITS_LOC3,
        SUM(CASE WHEN LOC = 4 THEN UNITS ELSE 0 END)                            AS UNITS_LOC4,
        SUM(CASE WHEN LOC = 5 THEN UNITS ELSE 0 END)                            AS UNITS_LOC5,
        SUM(CASE WHEN LOC = 6 THEN UNITS ELSE 0 END)                            AS UNITS_LOC6,
        SUM(CASE WHEN LOC = 7 THEN UNITS ELSE 0 END)                            AS UNITS_LOC7,
        SUM(CASE WHEN LOC = 8 THEN UNITS ELSE 0 END)                            AS UNITS_LOC8,
        SUM(CASE WHEN LOC = 9 THEN UNITS ELSE 0 END)                            AS UNITS_LOC9
    FROM OrderItemByLoc
    GROUP BY ITEM_NUMBER
),

InvEnd AS (
    -- Live companywide availability (includes loc 51 warehouse/staging)
    -- Matches the IMINQ01 screen; IBQOO = open SO qty, IBQAL = allocated picks
    SELECT
        IB.IBITEM AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN (IB.IBQOH - IB.IBQOO - IB.IBQAL)
                ELSE (IB.IBQOH - IB.IBQOO - IB.IBQAL) * IM.IMFACT
            END
        ) AS END_AVAIL
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 7, 8, 9, 51)
    GROUP BY IB.IBITEM
),

InvEndByLoc AS (
    -- Live per-location availability from ITEMBAL (sales locations only, no loc 51)
    SELECT
        X.ITEM_NUMBER,
        SUM(CASE WHEN X.IBLOC = 1 THEN X.AVAIL_SALES_UOM ELSE 0 END)           AS LOC1_AVAIL,
        SUM(CASE WHEN X.IBLOC = 3 THEN X.AVAIL_SALES_UOM ELSE 0 END)           AS LOC3_AVAIL,
        SUM(CASE WHEN X.IBLOC = 4 THEN X.AVAIL_SALES_UOM ELSE 0 END)           AS LOC4_AVAIL,
        SUM(CASE WHEN X.IBLOC = 5 THEN X.AVAIL_SALES_UOM ELSE 0 END)           AS LOC5_AVAIL,
        SUM(CASE WHEN X.IBLOC = 6 THEN X.AVAIL_SALES_UOM ELSE 0 END)           AS LOC6_AVAIL,
        SUM(CASE WHEN X.IBLOC = 7 THEN X.AVAIL_SALES_UOM ELSE 0 END)           AS LOC7_AVAIL,
        SUM(CASE WHEN X.IBLOC = 8 THEN X.AVAIL_SALES_UOM ELSE 0 END)           AS LOC8_AVAIL,
        SUM(CASE WHEN X.IBLOC = 9 THEN X.AVAIL_SALES_UOM ELSE 0 END)           AS LOC9_AVAIL
    FROM (
        SELECT
            IB.IBITEM AS ITEM_NUMBER,
            IB.IBLOC  AS IBLOC,
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN (IB.IBQOH - IB.IBQOO - IB.IBQAL)
                ELSE (IB.IBQOH - IB.IBQOO - IB.IBQAL) * IM.IMFACT
            END AS AVAIL_SALES_UOM
        FROM GSFL2K.ITEMBAL IB
        JOIN GSFL2K.ITEMMAST IM
          ON IM.IMITEM = IB.IBITEM
        WHERE IB.IBCO = 1
          AND IB.IBLOC IN (1, 3, 4, 5, 6, 7, 8, 9)
    ) X
    GROUP BY X.ITEM_NUMBER
)

SELECT
    TRIM(IM.IMITEM)                                                              AS "Item Number",
    TRIM(IM.IMDESC)                                                              AS "Description",
    TRIM(IX.IMCOLLECT)                                                           AS "Collection",
    IM.IMP1                                                                      AS "Sale Price",
    TRIM(IM.IMDIV)                                                               AS "Division",
    TRIM(IM.IMVEND)                                                              AS "Vendor Number",
    TRIM(VM.VMNAME)                                                              AS "Vendor Name",
    DECIMAL(COALESCE(IEN.END_AVAIL, 0), 18, 2)                                  AS "Current Inventory Available",
    TRIM(IM.IMUM2)                                                               AS "Sales UOM",

    -- ── Location 1 ──────────────────────────────────────────────────────────
    DECIMAL(COALESCE(LOC.LOC1_AVAIL, 0), 18, 2)                                 AS "Loc 1 - Avail",
    DECIMAL(COALESCE(SA.REV_LOC1,    0), 18, 2)                                 AS "Loc 1 - Revenue 365D",
    COALESCE(SA.ORDERS_LOC1, 0)                                                  AS "Loc 1 - Orders 365D",
    DECIMAL(COALESCE(SA.UNITS_LOC1,  0), 18, 2)                                 AS "Loc 1 - Units 365D",

    -- ── Location 3 ──────────────────────────────────────────────────────────
    DECIMAL(COALESCE(LOC.LOC3_AVAIL, 0), 18, 2)                                 AS "Loc 3 - Avail",
    DECIMAL(COALESCE(SA.REV_LOC3,    0), 18, 2)                                 AS "Loc 3 - Revenue 365D",
    COALESCE(SA.ORDERS_LOC3, 0)                                                  AS "Loc 3 - Orders 365D",
    DECIMAL(COALESCE(SA.UNITS_LOC3,  0), 18, 2)                                 AS "Loc 3 - Units 365D",

    -- ── Location 4 ──────────────────────────────────────────────────────────
    DECIMAL(COALESCE(LOC.LOC4_AVAIL, 0), 18, 2)                                 AS "Loc 4 - Avail",
    DECIMAL(COALESCE(SA.REV_LOC4,    0), 18, 2)                                 AS "Loc 4 - Revenue 365D",
    COALESCE(SA.ORDERS_LOC4, 0)                                                  AS "Loc 4 - Orders 365D",
    DECIMAL(COALESCE(SA.UNITS_LOC4,  0), 18, 2)                                 AS "Loc 4 - Units 365D",

    -- ── Location 5 ──────────────────────────────────────────────────────────
    DECIMAL(COALESCE(LOC.LOC5_AVAIL, 0), 18, 2)                                 AS "Loc 5 - Avail",
    DECIMAL(COALESCE(SA.REV_LOC5,    0), 18, 2)                                 AS "Loc 5 - Revenue 365D",
    COALESCE(SA.ORDERS_LOC5, 0)                                                  AS "Loc 5 - Orders 365D",
    DECIMAL(COALESCE(SA.UNITS_LOC5,  0), 18, 2)                                 AS "Loc 5 - Units 365D",

    -- ── Location 6 ──────────────────────────────────────────────────────────
    DECIMAL(COALESCE(LOC.LOC6_AVAIL, 0), 18, 2)                                 AS "Loc 6 - Avail",
    DECIMAL(COALESCE(SA.REV_LOC6,    0), 18, 2)                                 AS "Loc 6 - Revenue 365D",
    COALESCE(SA.ORDERS_LOC6, 0)                                                  AS "Loc 6 - Orders 365D",
    DECIMAL(COALESCE(SA.UNITS_LOC6,  0), 18, 2)                                 AS "Loc 6 - Units 365D",

    -- ── Location 7 ──────────────────────────────────────────────────────────
    DECIMAL(COALESCE(LOC.LOC7_AVAIL, 0), 18, 2)                                 AS "Loc 7 - Avail",
    DECIMAL(COALESCE(SA.REV_LOC7,    0), 18, 2)                                 AS "Loc 7 - Revenue 365D",
    COALESCE(SA.ORDERS_LOC7, 0)                                                  AS "Loc 7 - Orders 365D",
    DECIMAL(COALESCE(SA.UNITS_LOC7,  0), 18, 2)                                 AS "Loc 7 - Units 365D",

    -- ── Location 8 ──────────────────────────────────────────────────────────
    DECIMAL(COALESCE(LOC.LOC8_AVAIL, 0), 18, 2)                                 AS "Loc 8 - Avail",
    DECIMAL(COALESCE(SA.REV_LOC8,    0), 18, 2)                                 AS "Loc 8 - Revenue 365D",
    COALESCE(SA.ORDERS_LOC8, 0)                                                  AS "Loc 8 - Orders 365D",
    DECIMAL(COALESCE(SA.UNITS_LOC8,  0), 18, 2)                                 AS "Loc 8 - Units 365D",

    -- ── Location 9 ──────────────────────────────────────────────────────────
    DECIMAL(COALESCE(LOC.LOC9_AVAIL, 0), 18, 2)                                 AS "Loc 9 - Avail",
    DECIMAL(COALESCE(SA.REV_LOC9,    0), 18, 2)                                 AS "Loc 9 - Revenue 365D",
    COALESCE(SA.ORDERS_LOC9, 0)                                                  AS "Loc 9 - Orders 365D",
    DECIMAL(COALESCE(SA.UNITS_LOC9,  0), 18, 2)                                 AS "Loc 9 - Units 365D"

FROM GSFL2K.ITEMMAST IM
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = IM.IMVEND
INNER JOIN SalesAgg SA                          -- limits results to items with sales in window
  ON SA.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN InvEnd IEN
  ON IEN.ITEM_NUMBER = IM.IMITEM
LEFT JOIN InvEndByLoc LOC
  ON LOC.ITEM_NUMBER = IM.IMITEM

ORDER BY SA.TOTAL_REV DESC
FOR READ ONLY
