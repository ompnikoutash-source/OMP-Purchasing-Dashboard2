/* ============================================================
   DIVISION 5 FLOORING COLLECTIONS — INVENTORY & SALES REPORT
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Columns:
     - Item Number, Description, Collection
     - Available Companywide  (IBQOH - IBQAL, sales UOM, locs 1/3/4/5/6/8/9/51)
     - Qty on PO              (POLINE open lines, PLQORD - PLQREC, sales UOM)
     - Qty on Backorder       (IBQBO, sales UOM)
     - Units Sold 30 / 90 / 365 days  (SLBLUO from SHLINE)

   Collections filtered (ITEMXTRA.IMCOLLECT), Division 5 only.
   IMFACT applied to convert stocking UOM → sales UOM where they differ.
   ============================================================ */

WITH

SALES AS (
    SELECT
        L.SLITEM,
        SUM(CASE WHEN H.SHIDAT >= (CURRENT_DATE - 30 DAYS)  THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_30D,
        SUM(CASE WHEN H.SHIDAT >= (CURRENT_DATE - 90 DAYS)  THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_90D,
        SUM(CASE WHEN H.SHIDAT >= (CURRENT_DATE - 365 DAYS) THEN COALESCE(L.SLBLUO, 0) ELSE 0 END) AS UNITS_365D
    FROM GSFL2K.SHHEAD H
    JOIN GSFL2K.SHLINE L
        ON L.SLCO   = H.SHCO
       AND L.SLLOC  = H.SHLOC
       AND L.SLINV# = H.SHINV#
       AND L.SLORD# = H.SHORD#
    WHERE H.SHCO   = 1
      AND H.SHIDAT >= (CURRENT_DATE - 365 DAYS)
    GROUP BY L.SLITEM
),

INVENTORY AS (
    -- IBQOH and IBQAL are in stocking UOM (IMUM1).
    -- IMFACT converts to sales UOM (IMUM2) when the two differ.
    SELECT
        IB.IBITEM,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN CASE WHEN (IB.IBQOH - IB.IBQAL) > 0 THEN (IB.IBQOH - IB.IBQAL) ELSE 0 END
                ELSE CASE WHEN (IB.IBQOH - IB.IBQAL) > 0 THEN (IB.IBQOH - IB.IBQAL) ELSE 0 END * IM.IMFACT
            END
        ) AS AVAILABLE_COMPANYWIDE,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN COALESCE(IB.IBQBO, 0)
                ELSE COALESCE(IB.IBQBO, 0) * IM.IMFACT
            END
        ) AS QTY_ON_BACKORDER
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
        ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY IB.IBITEM
),

QTY_ON_PO AS (
    -- PLQORD - PLQREC = remaining open qty per line (stocking UOM / IMUM1).
    -- IMFACT converts to sales UOM the same way availability columns do.
    -- Filtered by PLDELT <> 'D' (not deleted) and open quantity remaining.
    -- No POHEAD join — PHSTAT does not exist on POHEAD in this system.
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
)

SELECT
    TRIM(I.IMITEM)     AS "Item Number",
    TRIM(I.IMDESC)     AS "Description",
    DECIMAL(COALESCE(INV.AVAILABLE_COMPANYWIDE, 0), 18, 2) AS "Available Companywide",
    DECIMAL(COALESCE(PO.TOTAL_QTY_ON_PO,        0), 18, 2) AS "Qty on PO",
    DECIMAL(COALESCE(INV.QTY_ON_BACKORDER,       0), 18, 2) AS "Qty on Backorder",
    DECIMAL(COALESCE(S.UNITS_30D,               0), 18, 2) AS "Units Sold - 30D",
    DECIMAL(COALESCE(S.UNITS_90D,               0), 18, 2) AS "Units Sold - 90D",
    DECIMAL(COALESCE(S.UNITS_365D,              0), 18, 2) AS "Units Sold - 365D"
FROM GSFL2K.ITEMMAST I
LEFT JOIN INVENTORY INV
    ON INV.IBITEM = I.IMITEM
LEFT JOIN QTY_ON_PO PO
    ON PO.ITEM_NUMBER = TRIM(I.IMITEM)
LEFT JOIN SALES S
    ON S.SLITEM = I.IMITEM
WHERE TRIM(I.IMITEM) IN (
        'GCSETCB-1724',
        'GCKEYSET16',
        'GCKEYSET74',
        'GCSETCC-1724',
        'GCSETCV-1724',
        'GCSETCVA-1724',
        'GCSETGL-2828',
        'GCSETGL-2828-1',
        'GCSETGL-2828-2',
        'GCSETII-1724',
        'GCSETII-1724B',
        'GCSETLE-2628',
        'GCSETNP-2228',
        'GCSETPS-2228',
        'GCSETP2-2428',
        'GCSETSS-1724',
        'GCSETEX-1724',
        'GCSETAL-2228',
        'GCSETAL9-2828',
        'GCSETBH-2828'
    )
ORDER BY TRIM(I.IMITEM)
