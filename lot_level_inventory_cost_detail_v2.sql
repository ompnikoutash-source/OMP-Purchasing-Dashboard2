/* ============================================================
   PRICING AUDIT ITEM-LEVEL COST SUMMARY
   DB: DB2/AS400 via ODBC, schema GSFL2K

   One row per item for Excel VLOOKUP/XLOOKUP use.

   Key fixes vs the prior lot-level query:
     - Uses location 1 ITEMBAL.IBLCST for average inventory cost, matching
       the Gartman location-1 screen used for unfinished PO decisions.
     - Uses ITEMRECH.IRCOST for the most recent PO landed cost.
     - Also keeps Gartman's item header Landed/Average cost and live-lot
       rollups as diagnostic columns, because they can diverge by SKU.
     - Uses cost fields by themselves. IRCOST/IDCOST already include freight;
       do not add IRFFAC again.
     - Keeps freight/velocity context out of the cost calculation so the
       pricing decision is auditable and not double-counted.

   Notes on the older item-level query:
     - It had useful velocity context: monthly average SF, avg SF per PO,
       and avg days between PO receipts.
     - It did not contain an explicit price-decay formula. The only decay
       logic found in this repo is a forecasting fallback that reduces demand
       after trailing zero weeks; it should not be silently applied to prices.
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

ReportItems AS (
    SELECT
        TRIM(IM.IMITEM)     AS ITEM_NUMBER,
        TRIM(IM.IMDESC)     AS DESCRIPTION,
        TRIM(IX.IMCOLLECT)  AS COLLECTION,
        TRIM(IM.IMDIV)      AS DIVISION,
        TRIM(IM.IMVEND)     AS VENDOR_NUMBER,
        TRIM(IM.IMUM1)      AS STOCKING_UOM,
        TRIM(IM.IMUM2)      AS SALES_UOM,
        IM.IMFACT           AS UOM_FACTOR,
        IM.IMP1             AS CURRENT_SALE_PRICE,
        IM.IMCOST           AS GARTMAN_LANDED_COST,
        IM.IMACST           AS GARTMAN_AVERAGE_COST
    FROM GSFL2K.ITEMMAST IM
    JOIN GSFL2K.ITEMXTRA IX
      ON IX.IMXITM = IM.IMITEM
    WHERE TRIM(IX.IMCOLLECT) IN ('REGULAR UNF', 'UNF. THE "6"')
),

InvEnd AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0))
                ELSE (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0)) * IM.IMFACT
            END
        ) AS END_AVAIL,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN IB.IBQOH
                ELSE IB.IBQOH * IM.IMFACT
            END
        ) AS ON_HAND
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY TRIM(IB.IBITEM)
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
            TRIM(IB.IBITEM) AS ITEM_NUMBER,
            IB.IBLOC,
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
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

Backorders AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN COALESCE(IB.IBQBO, 0)
                ELSE COALESCE(IB.IBQBO, 0) * IM.IMFACT
            END
        ) AS TOTAL_BACKORDER
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY TRIM(IB.IBITEM)
),

Loc1AvgInvCost AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        DECIMAL(
            MAX(CASE WHEN IB.IBLCST IS NOT NULL AND IB.IBLCST <> 0 THEN IB.IBLCST END),
            18,
            5
        ) AS LOC1_AVG_INVENTORY_COST,
        SUM(IB.IBQOH) AS LOC1_ON_HAND_QTY,
        SUM(IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0)) AS LOC1_AVAILABLE_QTY
    FROM GSFL2K.ITEMBAL IB
    WHERE IB.IBCO = 1
      AND IB.IBLOC = 1
    GROUP BY TRIM(IB.IBITEM)
),

LatestLandedCost AS (
    SELECT
        TRIM(R.IRITEM)           AS ITEM_NUMBER,
        R.IRDATE                 AS MOST_RECENT_PO_DATE,
        DECIMAL(R.IRCOST, 18, 5) AS MOST_RECENT_PO_COST,
        TRIM(CHAR(R.IRVEND))     AS MOST_RECENT_PO_VENDOR
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

AvgInvCost AS (
    SELECT
        TRIM(D.IDITEM) AS ITEM_NUMBER,
        DECIMAL(
            DECIMAL(SUM(
                DECIMAL(D.IDCOST, 18, 6) * DECIMAL(D.IDQOH, 18, 6)
            ), 18, 6)
            /
            NULLIF(
                DECIMAL(SUM(DECIMAL(D.IDQOH, 18, 6)), 18, 6),
                0
            ),
            18, 5
        ) AS AVG_INVENTORY_COST,
        SUM(D.IDQOH) AS AVG_COST_STOCKING_QTY
    FROM GSFL2K.ITEMDETL D
    WHERE D.IDCO = 1
      AND D.IDLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
      AND COALESCE(D.IDDELT, '') <> 'D'
      AND D.IDQOH > 0
      AND D.IDCOST IS NOT NULL
      AND D.IDCOST <> 0
    GROUP BY TRIM(D.IDITEM)
),

LiveLotRows AS (
    SELECT
        TRIM(D.IDITEM) AS ITEM_NUMBER,
        D.IDDATE,
        DECIMAL(D.IDCOST, 18, 5) AS IDCOST,
        ROW_NUMBER() OVER (
            PARTITION BY TRIM(D.IDITEM)
            ORDER BY D.IDDATE ASC, D.IDSERL ASC, D.IDLOC ASC, D.IDCOST ASC
        ) AS OLDEST_RN,
        ROW_NUMBER() OVER (
            PARTITION BY TRIM(D.IDITEM)
            ORDER BY D.IDDATE DESC, D.IDSERL DESC, D.IDLOC DESC, D.IDCOST DESC
        ) AS NEWEST_RN
    FROM GSFL2K.ITEMDETL D
    WHERE D.IDCO = 1
      AND D.IDLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
      AND COALESCE(D.IDDELT, '') <> 'D'
      AND D.IDQOH > 0
      AND D.IDCOST IS NOT NULL
      AND D.IDCOST <> 0
),

LiveLotPosition AS (
    SELECT
        ITEM_NUMBER,
        MIN(IDDATE) AS OLDEST_LIVE_LOT_DATE,
        MAX(IDDATE) AS NEWEST_LIVE_LOT_DATE
    FROM LiveLotRows
    GROUP BY ITEM_NUMBER
),

OldestLiveLotCost AS (
    SELECT
        ITEM_NUMBER,
        IDDATE AS OLDEST_LIVE_LOT_DATE,
        IDCOST AS OLDEST_LIVE_LOT_COST
    FROM LiveLotRows
    WHERE OLDEST_RN = 1
),

NewestLiveLotCost AS (
    SELECT
        ITEM_NUMBER,
        IDDATE AS NEWEST_LIVE_LOT_DATE,
        IDCOST AS NEWEST_LIVE_LOT_COST
    FROM LiveLotRows
    WHERE NEWEST_RN = 1
),

SnapDates AS (
    SELECT
        (SELECT MIN(E.IBEODDATE)
         FROM GSFL2K.ITEMBA_EOD E
         JOIN Params P ON 1=1
         WHERE E.IBCO = 1
           AND E.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
           AND E.IBEODDATE >= P.D90
        ) AS BEGIN_SNAP_DATE
    FROM SYSIBM.SYSDUMMY1
),

InvBegin AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN (IB.IBDBOH - IB.IBQOO)
                ELSE (IB.IBDBOH - IB.IBQOO) * IM.IMFACT
            END
        ) AS BEGIN_AVAIL
    FROM SnapDates SD
    JOIN GSFL2K.ITEMBA_EOD IB
      ON IB.IBEODDATE = SD.BEGIN_SNAP_DATE
     AND IB.IBCO = 1
     AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    GROUP BY TRIM(IB.IBITEM)
),

OrderItem365 AS (
    SELECT
        TRIM(L.SLITEM) AS ITEM_NUMBER,
        H.SHIDAT AS SHIP_DATE,
        (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#))) AS INVKEY,
        SUM(COALESCE(L.SLENET,0)) AS INV_ITEM_SALES,
        SUM(COALESCE(L.SLBLUO,0)) AS INV_ITEM_UNITS
    FROM Params P
    JOIN GSFL2K.SHHEAD H
      ON H.SHIDAT >= P.D365
    JOIN GSFL2K.SHLINE L
      ON L.SLCO   = H.SHCO
     AND L.SLLOC  = H.SHLOC
     AND L.SLINV# = H.SHINV#
     AND L.SLORD# = H.SHORD#
    GROUP BY
        TRIM(L.SLITEM),
        H.SHIDAT,
        (RTRIM(CHAR(H.SHCO)) || '|' || RTRIM(CHAR(H.SHLOC)) || '|' || RTRIM(CHAR(H.SHINV#)))
),

SalesAgg AS (
    SELECT
        ITEM_NUMBER,
        SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 30 DAYS)  THEN INV_ITEM_SALES ELSE 0 END) AS SALES_30D,
        SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 90 DAYS)  THEN INV_ITEM_SALES ELSE 0 END) AS SALES_90D,
        SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 180 DAYS) THEN INV_ITEM_SALES ELSE 0 END) AS SALES_180D,
        SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 365 DAYS) THEN INV_ITEM_SALES ELSE 0 END) AS SALES_365D,
        COUNT(DISTINCT CASE WHEN SHIP_DATE >= (CURRENT_DATE - 30 DAYS)  THEN INVKEY END) AS ORDERS_30D,
        COUNT(DISTINCT CASE WHEN SHIP_DATE >= (CURRENT_DATE - 90 DAYS)  THEN INVKEY END) AS ORDERS_90D,
        COUNT(DISTINCT CASE WHEN SHIP_DATE >= (CURRENT_DATE - 180 DAYS) THEN INVKEY END) AS ORDERS_180D,
        COUNT(DISTINCT CASE WHEN SHIP_DATE >= (CURRENT_DATE - 365 DAYS) THEN INVKEY END) AS ORDERS_365D,
        SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 30 DAYS)  THEN INV_ITEM_UNITS ELSE 0 END) AS UNITS_30D,
        SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 90 DAYS)  THEN INV_ITEM_UNITS ELSE 0 END) AS UNITS_90D,
        SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 180 DAYS) THEN INV_ITEM_UNITS ELSE 0 END) AS UNITS_180D,
        SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 365 DAYS) THEN INV_ITEM_UNITS ELSE 0 END) AS UNITS_365D,
        DECIMAL(
            SUM(CASE WHEN SHIP_DATE >= (CURRENT_DATE - 4 MONTHS)
                      AND SHIP_DATE < CURRENT_DATE
                     THEN INV_ITEM_UNITS ELSE 0 END) / 4.0,
            18, 2
        ) AS MONTHLY_AVG_UNITS_4MO
    FROM OrderItem365
    GROUP BY ITEM_NUMBER
),

POReceiptEvents AS (
    SELECT
        TRIM(R.IRITEM) AS ITEM_NUMBER,
        R.IRRECNBR,
        MIN(R.IRDATE) AS REC_DATE,
        SUM(
            DECIMAL(R.IRQTY, 18, 4)
            * CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN 1
                ELSE IM.IMFACT
              END
        ) AS EVENT_SALES_UOM_QTY
    FROM GSFL2K.ITEMRECH R
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = R.IRITEM
    WHERE R.IRCO = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRQTY > 0
      AND R.IRRECNBR > 0
      AND R.IRDATE >= CURRENT_DATE - 1825 DAYS
    GROUP BY TRIM(R.IRITEM), R.IRRECNBR
),

POStats AS (
    SELECT
        ITEM_NUMBER,
        DECIMAL(AVG(DECIMAL(EVENT_SALES_UOM_QTY, 18, 2)), 18, 2) AS AVG_SALES_UOM_PER_PO
    FROM POReceiptEvents
    GROUP BY ITEM_NUMBER
),

POReceiptDates AS (
    SELECT
        ITEM_NUMBER,
        REC_DATE,
        ROW_NUMBER() OVER (
            PARTITION BY ITEM_NUMBER
            ORDER BY REC_DATE, IRRECNBR
        ) AS RN
    FROM POReceiptEvents
),

PODaysBetween AS (
    SELECT
        A.ITEM_NUMBER,
        DECIMAL(AVG(DECIMAL(DAYS(B.REC_DATE) - DAYS(A.REC_DATE), 18, 1)), 18, 1) AS AVG_DAYS_BETWEEN_PO
    FROM POReceiptDates A
    JOIN POReceiptDates B
      ON A.ITEM_NUMBER = B.ITEM_NUMBER
     AND B.RN = A.RN + 1
    GROUP BY A.ITEM_NUMBER
)

SELECT
    RI.COLLECTION                                                  AS "Collection",
    RI.ITEM_NUMBER                                                AS "Item Number",
    RI.DESCRIPTION                                                AS "Description",
    RI.CURRENT_SALE_PRICE                                         AS "Current Sale Price",
    RI.DIVISION                                                   AS "Division",
    COALESCE(LC.MOST_RECENT_PO_VENDOR, RI.VENDOR_NUMBER)          AS "Vendor Number",
    COALESCE(TRIM(LPVM.VMNAME), TRIM(VM.VMNAME))                  AS "Vendor Name",
    DECIMAL(COALESCE(IEN.END_AVAIL, 0), 18, 2)                    AS "Current Inventory Available",
    DECIMAL(COALESCE(IEN.ON_HAND, 0), 18, 2)                      AS "Current Inventory On Hand",
    DECIMAL(COALESCE(BO.TOTAL_BACKORDER, 0), 18, 2)               AS "Backorders",
    RI.SALES_UOM                                                  AS "Sales Unit of Measurement",
    RI.STOCKING_UOM                                               AS "Stocking Unit of Measurement",
    RI.UOM_FACTOR                                                 AS "UOM Factor",

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

    DECIMAL(COALESCE(LC.MOST_RECENT_PO_COST, 0), 18, 5) AS "Landed Cost - Most Recent PO",
    DECIMAL(COALESCE(L1.LOC1_AVG_INVENTORY_COST, 0), 18, 5) AS "Landed Cost - Avg Inventory",
    DECIMAL(COALESCE(RI.GARTMAN_LANDED_COST, 0), 18, 5) AS "Gartman Header Landed Cost",
    CASE
        WHEN COALESCE(RI.CURRENT_SALE_PRICE, 0) = 0 OR LC.MOST_RECENT_PO_COST IS NULL THEN NULL
        ELSE DECIMAL((RI.CURRENT_SALE_PRICE - LC.MOST_RECENT_PO_COST) / RI.CURRENT_SALE_PRICE, 18, 6)
    END AS "Gross Margin % - Most Recent PO",
    CASE
        WHEN COALESCE(RI.CURRENT_SALE_PRICE, 0) = 0 OR L1.LOC1_AVG_INVENTORY_COST IS NULL THEN NULL
        ELSE DECIMAL((RI.CURRENT_SALE_PRICE - L1.LOC1_AVG_INVENTORY_COST) / RI.CURRENT_SALE_PRICE, 18, 6)
    END AS "Gross Margin % - Avg Inventory",

    DECIMAL(COALESCE(IBG.BEGIN_AVAIL, 0), 18, 2) AS "Begin Available (90D)",
    CASE
        WHEN (COALESCE(IBG.BEGIN_AVAIL, 0) + COALESCE(IEN.END_AVAIL, 0)) = 0 THEN NULL
        ELSE DECIMAL(
            DECIMAL(COALESCE(SA.UNITS_90D, 0), 18, 6) * DECIMAL(730, 18, 6)
            /
            NULLIF(
                (DECIMAL(COALESCE(IBG.BEGIN_AVAIL, 0), 18, 6) + DECIMAL(COALESCE(IEN.END_AVAIL, 0), 18, 6))
                * DECIMAL(90, 18, 6),
                0
            ),
            18, 4
        )
    END AS "Inventory Turnover - Annualized",

    LC.MOST_RECENT_PO_DATE                           AS "Last FOB Date",
    LC.MOST_RECENT_PO_VENDOR                         AS "Most Recent PO Vendor Number",
    TRIM(LPVM.VMNAME)                                AS "Most Recent PO Vendor Name",
    DECIMAL(COALESCE(SA.MONTHLY_AVG_UNITS_4MO, 0), 18, 2) AS "Monthly Avg Units - Last 4 Months",
    DECIMAL(COALESCE(PS.AVG_SALES_UOM_PER_PO, 0), 18, 2)  AS "Average Units per PO - Last 5 Years",
    DECIMAL(COALESCE(PDB.AVG_DAYS_BETWEEN_PO, 0), 18, 1)  AS "Average Days Between POs - Last 5 Years",
    DECIMAL(COALESCE(L1.LOC1_ON_HAND_QTY, 0), 18, 2) AS "Avg Cost Source Qty (Stocking UOM)",

    DECIMAL(COALESCE(LC.MOST_RECENT_PO_COST, 0), 18, 5) AS "Diagnostic - Latest Receipt Cost",
    DECIMAL(COALESCE(RI.GARTMAN_AVERAGE_COST, 0), 18, 5) AS "Diagnostic - Item Master Avg Cost",
    DECIMAL(COALESCE(AC.AVG_INVENTORY_COST, 0), 18, 5)  AS "Diagnostic - Live Lot Weighted Avg Cost",
    OLC.OLDEST_LIVE_LOT_DATE                            AS "Diagnostic - Oldest Live Lot Date",
    DECIMAL(COALESCE(OLC.OLDEST_LIVE_LOT_COST, 0), 18, 5) AS "Diagnostic - Oldest Live Lot Cost",
    NLC.NEWEST_LIVE_LOT_DATE                            AS "Diagnostic - Newest Live Lot Date",
    DECIMAL(COALESCE(NLC.NEWEST_LIVE_LOT_COST, 0), 18, 5) AS "Diagnostic - Newest Live Lot Cost"

FROM ReportItems RI
LEFT JOIN GSFL2K.VENDMAST VM
  ON TRIM(VM.VMVEND) = RI.VENDOR_NUMBER
LEFT JOIN InvEnd IEN
  ON IEN.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN InvEndByLoc LOC
  ON LOC.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN Backorders BO
  ON BO.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN LatestLandedCost LC
  ON LC.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN Loc1AvgInvCost L1
  ON L1.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN AvgInvCost AC
  ON AC.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN OldestLiveLotCost OLC
  ON OLC.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN NewestLiveLotCost NLC
  ON NLC.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN InvBegin IBG
  ON IBG.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN SalesAgg SA
  ON SA.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN POStats PS
  ON PS.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN PODaysBetween PDB
  ON PDB.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN GSFL2K.VENDMAST LPVM
  ON TRIM(LPVM.VMVEND) = LC.MOST_RECENT_PO_VENDOR

ORDER BY
    RI.ITEM_NUMBER

FOR READ ONLY
