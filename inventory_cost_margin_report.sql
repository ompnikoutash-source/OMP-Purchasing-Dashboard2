/* ============================================================
   INVENTORY COST & MARGIN REPORT
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Reports per item:
     - Current available inventory by location (sales UOM)
     - Sales $ and units over 30 / 90 / 180 / 365 days
     - Order counts over same windows
     - Landed Cost — Most Recent PO  (from ITEMRECH, most recent IRRECNBR)
     - Landed Cost — Avg Inventory   (weighted avg across on-hand tags via ITEMDETL)
     - Gross Margin % for both cost methods
     - Inventory Turnover — Annualized (90-day window)
     - Last FOB Date                  (date of most recent PO receipt)
     - Avg Days Between POs           (avg interval between PO receipts, 5-year window)

   COST COLUMN NOTES:
     "Landed Cost - Most Recent PO"
       Source: ITEMRECH with IRSRC='P' (true purchase receipts).
       Picks the row with the highest IRRECNBR per item — the most
       recent receipt event. IRCOST is stored in the sales UOM
       (IMUM2) — the same unit as IMP1. No IMFACT conversion is
       applied. Confirmed via diagnostic: items with IMUM1=BNDL /
       IMUM2=SF / IMFACT=16.88 store IRCOST per SF, not per bundle.

     "Landed Cost - Avg Inventory"
       Source: ITEMDETL, one row per on-hand tag/serial.
       IDCOST is the cost assigned to that tag at receipt time,
       stored in the sales UOM (IMUM2). It does NOT update on
       counts, transfers, or adjustments (unlike IDDATE).
       Formula: SUM(IDCOST * IDQOH) / SUM(IDQOH)
       = weighted-average cost per sales-UOM across all live tags.
       No IMFACT conversion is applied because IDCOST is already
       in the sales UOM.
       Filtered to locations 1,3,4,5,6,8,9,51 to match the
       availability columns (excludes loc 90 and other staging locs).

   INVENTORY AVAILABILITY NOTES:
     InvBegin: ITEMBA_EOD snapshot from ~90 days ago (IBDBOH - IBQOO).
       Used only in the turnover denominator; IBQAL is not in EOD.
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

SnapDates AS (
    SELECT
        (SELECT MIN(E.IBEODDATE)
         FROM GSFL2K.ITEMBA_EOD E
         JOIN Params P ON 1=1
         WHERE E.IBCO = 1
           AND E.IBLOC <> 90
           AND E.IBEODDATE >= P.D90
        ) AS BEGIN_SNAP_DATE
    FROM SYSIBM.SYSDUMMY1
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

InvBegin AS (
    SELECT
        IB.IBITEM AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
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
    GROUP BY IB.IBITEM
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

LatestLandedCost AS (
    -- IRCOST is stored in the sales UOM (IMUM2), the same unit as IMP1.
    -- No IMFACT conversion is applied — dividing by IMFACT was confirmed
    -- to produce incorrect results (e.g. ~$0.17 instead of ~$2.85/SF).
    SELECT
        TRIM(R.IRITEM) AS ITEM_NUMBER,
        R.IRDATE       AS MOST_RECENT_RECEIVED_DATE,
        DECIMAL(R.IRCOST, 18, 4) AS MOST_RECENT_LANDED_COST
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

-- IRFFAC = per-unit freight factor (DECIMAL 11,5). It is kept separate
-- from IDCOST because IDCOST already matches Gartman's landed Cost field.
-- IRFRAM is the total freight $ for the receipt line (2dp), not per-unit.
SerialFreight AS (
    SELECT
        TRIM(IRITEM)               AS ITEM_NUMBER,
        TRIM(IRSERL)               AS SERIAL,
        MAX(COALESCE(IRFFAC, 0))   AS FREIGHT
    FROM GSFL2K.ITEMRECH
    WHERE IRCO = 1
      AND TRIM(IRSRC) = 'P'
    GROUP BY TRIM(IRITEM), TRIM(IRSERL)
),

AvgInvCost AS (
    -- IDCOST is stored in the sales UOM (IMUM2). IDQOH is in stocking UOM
    -- (IMUM1). The weighted average simplifies to SUM(IDCOST * IDQOH) /
    -- SUM(IDQOH) because IMFACT cancels when cost is already per sales unit.
    SELECT
        TRIM(D.IDITEM) AS ITEM_NUMBER,
        DECIMAL(
            DECIMAL(SUM(
                DECIMAL(D.IDCOST, 18, 6) * DECIMAL(D.IDQOH, 18, 6)
            ), 18, 6) /
            NULLIF(
                DECIMAL(SUM(DECIMAL(D.IDQOH, 18, 6)), 18, 6),
                0
            ),
            18, 4
        ) AS AVG_INVENTORY_COST
    FROM GSFL2K.ITEMDETL D
    LEFT JOIN SerialFreight SF
      ON SF.ITEM_NUMBER = TRIM(D.IDITEM)
     AND SF.SERIAL      = TRIM(D.IDSERL)
    WHERE D.IDCO = 1
      AND D.IDLOC IN ('001', '003', '004', '005', '006', '008', '009', '051')
      AND COALESCE(D.IDDELT, '') <> 'D'
      AND D.IDQOH > 0
      AND D.IDCOST IS NOT NULL
      AND D.IDCOST <> 0
    GROUP BY TRIM(D.IDITEM)
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
    -- PLDELT <> 'D' mirrors the same delete-flag filter used in AvgInvCost.
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
    TRIM(IM.IMFMCD)    AS "Product Family Code",
    TRIM(FM.FMDESC)    AS "Product Family Name",
    TRIM(IM.IMVEND)    AS "Vendor Number",
    TRIM(VM.VMNAME)    AS "Vendor Name",

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

    DECIMAL(COALESCE(LC.MOST_RECENT_LANDED_COST, 0), 18, 4) AS "Landed Cost - Most Recent PO",
    DECIMAL(COALESCE(AC.AVG_INVENTORY_COST, 0), 18, 4)      AS "Landed Cost - Avg Inventory",

    CASE
        WHEN COALESCE(IM.IMP1, 0) = 0 OR LC.MOST_RECENT_LANDED_COST IS NULL THEN NULL
        ELSE DECIMAL(
            (IM.IMP1 - LC.MOST_RECENT_LANDED_COST) / IM.IMP1,
            18, 6
        )
    END AS "Gross Margin % - Most Recent PO",

    CASE
        WHEN COALESCE(IM.IMP1, 0) = 0 OR AC.AVG_INVENTORY_COST IS NULL THEN NULL
        ELSE DECIMAL(
            (IM.IMP1 - AC.AVG_INVENTORY_COST) / IM.IMP1,
            18, 6
        )
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

    LC.MOST_RECENT_RECEIVED_DATE                        AS "Last FOB Date",
    COALESCE(APF.AVG_DAYS_BETWEEN_POS, 365)             AS "Avg Days Between POs"

FROM GSFL2K.ITEMMAST IM
LEFT JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
LEFT JOIN GSFL2K.FAMILY FM
  ON TRIM(FM.FMFMCD) = TRIM(IM.IMFMCD)
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = IM.IMVEND
LEFT JOIN SalesAgg SA
  ON SA.ITEM_NUMBER = IM.IMITEM
LEFT JOIN InvBegin IBG
  ON IBG.ITEM_NUMBER = IM.IMITEM
LEFT JOIN InvEnd IEN
  ON IEN.ITEM_NUMBER = IM.IMITEM
LEFT JOIN Backorders BO
  ON BO.ITEM_NUMBER = IM.IMITEM
LEFT JOIN QtyOnPO PO
  ON PO.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN InvEndByLoc LOC
  ON LOC.ITEM_NUMBER = IM.IMITEM
LEFT JOIN VipProductPrice VIP_PRODUCT
  ON VIP_PRODUCT.PRICE_CODE = IM.IMPRCD
LEFT JOIN VipItemPrice VIP_ITEM
  ON VIP_ITEM.ITEM_NUMBER = TRIM(IM.IMITEM)
LEFT JOIN LatestLandedCost LC
  ON TRIM(LC.ITEM_NUMBER) = TRIM(IM.IMITEM)
LEFT JOIN AvgInvCost AC
  ON TRIM(AC.ITEM_NUMBER) = TRIM(IM.IMITEM)
LEFT JOIN AvgPOFrequency APF
  ON TRIM(APF.ITEM_NUMBER) = TRIM(IM.IMITEM)

ORDER BY IM.IMITEM
