-- Diagnostic: compare ITEMBA_EOD vs live ITEMBAL for GFBHO9501
-- Purpose: identify why inventory_cost_margin_report.sql availability
--          does not match the Gartman screen (OE01014 / IMINQ01).
--
-- Expected from Gartman (2026-06-24 live):
--   On hand = 2094 CTN, Committed = 928 CTN, Balance = 1166 CTN
--   Available SF = 1166 * 22.74 = 26,514.84

WITH
Item AS (SELECT 'GFBHO9501' AS ITEM FROM SYSIBM.SYSDUMMY1),

SnapDate AS (
    SELECT MAX(IB.IBEODDATE) AS SNAP_DATE
    FROM GSFL2K.ITEMBA_EOD IB
    JOIN Item I ON TRIM(IB.IBITEM) = I.ITEM
    WHERE IB.IBCO = 1
      AND IB.IBEODDATE <= CURRENT_DATE
),

-- ITEMBA_EOD fields: IBDBOH = on-hand, IBQOO = committed (semantics unclear)
EOD AS (
    SELECT
        IB.IBLOC,
        SD.SNAP_DATE,
        IB.IBDBOH                           AS EOD_ON_HAND_CTN,
        IB.IBQOO                            AS EOD_IBQOO_CTN,
        (IB.IBDBOH - IB.IBQOO)             AS EOD_BALANCE_CTN,
        IM.IMFACT,
        TRIM(IM.IMUM1) AS UM1,
        TRIM(IM.IMUM2) AS UM2,
        CASE WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
             THEN (IB.IBDBOH - IB.IBQOO)
             ELSE (IB.IBDBOH - IB.IBQOO) * IM.IMFACT
        END                                 AS EOD_BALANCE_SF
    FROM SnapDate SD
    JOIN GSFL2K.ITEMBA_EOD IB
      ON IB.IBEODDATE = SD.SNAP_DATE
     AND IB.IBCO = 1
    JOIN Item I ON TRIM(IB.IBITEM) = I.ITEM
    JOIN GSFL2K.ITEMMAST IM ON IM.IMITEM = IB.IBITEM
),

-- Live ITEMBAL: IBQOH = on-hand, IBQOO = open SO, IBQAL = allocated picks
LIVE AS (
    SELECT
        IB.IBLOC,
        IB.IBQOH                              AS LIVE_ON_HAND_CTN,
        IB.IBQOO                              AS LIVE_IBQOO_CTN,
        IB.IBQAL                              AS LIVE_IBQAL_CTN,
        (IB.IBQOO + IB.IBQAL)                AS LIVE_COMMITTED_CTN,
        (IB.IBQOH - IB.IBQOO - IB.IBQAL)    AS LIVE_BALANCE_CTN,
        IM.IMFACT,
        CASE WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
             THEN (IB.IBQOH - IB.IBQOO - IB.IBQAL)
             ELSE (IB.IBQOH - IB.IBQOO - IB.IBQAL) * IM.IMFACT
        END                                   AS LIVE_BALANCE_SF
    FROM GSFL2K.ITEMBAL IB
    JOIN Item I ON TRIM(IB.IBITEM) = I.ITEM
    JOIN GSFL2K.ITEMMAST IM ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
)

-- Detail by location
SELECT
    COALESCE(E.IBLOC, L.IBLOC)             AS LOCATION,
    E.SNAP_DATE,
    E.EOD_ON_HAND_CTN,
    E.EOD_IBQOO_CTN,
    E.EOD_BALANCE_CTN,
    E.EOD_BALANCE_SF,
    L.LIVE_ON_HAND_CTN,
    L.LIVE_IBQOO_CTN,
    L.LIVE_IBQAL_CTN,
    L.LIVE_COMMITTED_CTN,
    L.LIVE_BALANCE_CTN,
    L.LIVE_BALANCE_SF,
    (E.EOD_BALANCE_SF - L.LIVE_BALANCE_SF)  AS DIFF_SF
FROM EOD E
FULL OUTER JOIN LIVE L ON L.IBLOC = E.IBLOC

UNION ALL

-- Totals row — included locations only (1,3,4,5,6,8,9,51)
SELECT
    -1 AS LOCATION,
    MAX(E.SNAP_DATE),
    SUM(E.EOD_ON_HAND_CTN), SUM(E.EOD_IBQOO_CTN),
    SUM(E.EOD_BALANCE_CTN), SUM(E.EOD_BALANCE_SF),
    SUM(L.LIVE_ON_HAND_CTN), SUM(L.LIVE_IBQOO_CTN),
    SUM(L.LIVE_IBQAL_CTN),  SUM(L.LIVE_COMMITTED_CTN),
    SUM(L.LIVE_BALANCE_CTN), SUM(L.LIVE_BALANCE_SF),
    SUM(E.EOD_BALANCE_SF - L.LIVE_BALANCE_SF)
FROM EOD E
FULL OUTER JOIN LIVE L ON L.IBLOC = E.IBLOC
WHERE COALESCE(E.IBLOC, L.IBLOC) IN (1, 3, 4, 5, 6, 8, 9, 51)

ORDER BY LOCATION
