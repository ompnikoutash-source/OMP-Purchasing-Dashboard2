/* ============================================================
   SAMPLE SETS INVENTORY REPORT
   Columns: Item, Description, Collection, Vendor, On Hand,
            Available, Backorders, On PO, Last Received,
            Units Sold 365D
   Filter:  Specific sample set item numbers only
   ============================================================ */

WITH

TargetItems AS (
    SELECT ITEM_NUMBER FROM (VALUES
        ('GCSETAL-2228'),
        ('GCSETAL9-2828'),
        ('GCSETBH-2828'),
        ('GCSETCB-1724'),
        ('GCSETCC-1724'),
        -- ('GCSET??-????'),   -- TODO: Contractors Choice — item number unknown
        ('GCSETCV-1724'),
        ('GCSETCVA-1724'),
        ('GCSETEX-1724'),
        ('GCSETGL-2828'),
        ('GCSETII-1724'),
        -- ('GCSET??-????'),   -- TODO: Garrison II Smooth — item number unknown
        ('GCSETNP-2228'),
        ('GCSETP2-2428'),
        ('GCSETPS-2228'),
        ('GCSETSS-1724'),
        ('PFSHSA02')
    ) AS T(ITEM_NUMBER)
),

Units365 AS (
    SELECT
        L.SLITEM                   AS ITEM_NUMBER,
        SUM(COALESCE(L.SLBLUO, 0)) AS UNITS_365D
    FROM GSFL2K.SHHEAD H
    JOIN GSFL2K.SHLINE L
      ON L.SLCO   = H.SHCO
     AND L.SLLOC  = H.SHLOC
     AND L.SLINV# = H.SHINV#
     AND L.SLORD# = H.SHORD#
    WHERE H.SHIDAT >= (CURRENT_DATE - 365 DAYS)
    GROUP BY L.SLITEM
),

InvSummary AS (
    SELECT
        IB.IBITEM AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN COALESCE(IB.IBDBOH, 0)
                ELSE COALESCE(IB.IBDBOH, 0) * IM.IMFACT
            END
        ) AS ON_HAND,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN (COALESCE(IB.IBDBOH, 0) - COALESCE(IB.IBQOO, 0))
                ELSE (COALESCE(IB.IBDBOH, 0) - COALESCE(IB.IBQOO, 0)) * IM.IMFACT
            END
        ) AS AVAILABLE,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN COALESCE(IB.IBQBO, 0)
                ELSE COALESCE(IB.IBQBO, 0) * IM.IMFACT
            END
        ) AS BACKORDERS,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN COALESCE(IB.IBQOOV, 0)
                ELSE COALESCE(IB.IBQOOV, 0) * IM.IMFACT
            END
        ) AS ON_PO
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY IB.IBITEM
),

LastReceived AS (
    SELECT
        TRIM(R.IRITEM) AS ITEM_NUMBER,
        MAX(R.IRDATE)  AS LAST_RECEIVED_DATE
    FROM GSFL2K.ITEMRECH R
    WHERE R.IRCO     = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRQTY    > 0
      AND R.IRRECNBR > 0
    GROUP BY TRIM(R.IRITEM)
)

SELECT
    TRIM(IM.IMITEM)                                  AS "Item Number",
    TRIM(IM.IMDESC)                                  AS "Description",
    DECIMAL(COALESCE(INV.ON_HAND,    0), 18, 2)      AS "On Hand",
    DECIMAL(COALESCE(INV.AVAILABLE,  0), 18, 2)      AS "Available",
    DECIMAL(COALESCE(INV.BACKORDERS, 0), 18, 2)      AS "Backorders",
    DECIMAL(COALESCE(INV.ON_PO,      0), 18, 2)      AS "On PO",
    LR.LAST_RECEIVED_DATE                            AS "Last Received Date",
    COALESCE(U.UNITS_365D, 0)                        AS "Units Sold - 365D"

FROM TargetItems TI
JOIN GSFL2K.ITEMMAST IM
  ON TRIM(IM.IMITEM) = TI.ITEM_NUMBER
LEFT JOIN InvSummary INV
  ON INV.ITEM_NUMBER = IM.IMITEM
LEFT JOIN LastReceived LR
  ON LR.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN Units365 U
  ON U.ITEM_NUMBER = IM.IMITEM

ORDER BY TRIM(IM.IMITEM)
