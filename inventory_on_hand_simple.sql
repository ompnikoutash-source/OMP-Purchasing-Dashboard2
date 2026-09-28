/* ============================================================
   INVENTORY ON HAND — SIMPLE SNAPSHOT
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Returns one row per item with companywide on-hand quantity
   in the item's inventory (stocking) unit of measure.

   Columns:
     Item Number    -- IM.IMITEM
     Description    -- IM.IMDESC
     Qty On Hand    -- SUM(IB.IBDBOH) across active locations,
                       in inventory UOM (IMUM1) — no IMFACT
                       conversion applied
     Inventory UOM  -- IM.IMUM1

   Locations included: 1, 3, 4, 5, 6, 8, 9, 51
   Snapshot date: most recent IBEODDATE <= today
   ============================================================ */

WITH
LatestSnap AS (
    SELECT MAX(IB.IBEODDATE) AS SNAP_DATE
    FROM GSFL2K.ITEMBA_EOD IB
    WHERE IB.IBCO    = 1
      AND IB.IBLOC  <> 90
      AND IB.IBEODDATE <= CURRENT_DATE
),

InvOnHand AS (
    SELECT
        IB.IBITEM                AS ITEM_NUMBER,
        SUM(IB.IBDBOH)           AS QTY_ON_HAND
    FROM LatestSnap S
    JOIN GSFL2K.ITEMBA_EOD IB
      ON IB.IBEODDATE = S.SNAP_DATE
     AND IB.IBCO  = 1
     AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY IB.IBITEM
)

SELECT
    TRIM(IM.IMITEM)                                AS "Item Number",
    TRIM(IM.IMDESC)                                AS "Description",
    DECIMAL(COALESCE(INV.QTY_ON_HAND, 0), 18, 2)  AS "Qty On Hand",
    TRIM(IM.IMUM1)                                 AS "Inventory UOM"
FROM GSFL2K.ITEMMAST IM
LEFT JOIN InvOnHand INV
  ON INV.ITEM_NUMBER = IM.IMITEM
ORDER BY IM.IMITEM
