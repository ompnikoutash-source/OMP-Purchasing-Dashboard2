/* ============================================================
   CONTRACTOR'S CHOICE - SALES HISTORY AND AVAILABLE INVENTORY
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Purpose:
     One row per Contractor's Choice item showing current combined
     available SF and total customer sales SF over the past 2 years.

   Current available SF:
     - Source: ITEMBAL.
     - Locations included: company 1 locations 1, 3, 4, 5, 6, 8, 9, 51.
     - Formula: IBQOH - IBQOO - IBQAL, converted to SF with IMFACT when
       stocking UOM differs from sales UOM.

   Sales SF:
     - Source: SHHEAD / SHLINE invoice history.
     - Uses SLBLUO, the billing/sales UOM quantity.
     - Excludes transfer and internal accounts so customer demand is not
       inflated by branch movement or internal adjustments.
   ============================================================ */

WITH

ReportItems AS (
    SELECT
        TRIM(IM.IMITEM) AS ITEM_NUMBER,
        TRIM(IM.IMDESC) AS DESCRIPTION,
        DECIMAL(IM.IMP1, 18, 5) AS SALE_PRICE,
        TRIM(IM.IMUM1) AS STOCKING_UOM,
        TRIM(IM.IMUM2) AS SALES_UOM,
        COALESCE(IM.IMFACT, 0) AS UOM_FACTOR
    FROM GSFL2K.ITEMMAST IM
    JOIN GSFL2K.ITEMXTRA IX
      ON IX.IMXITM = IM.IMITEM
    WHERE UPPER(TRIM(IX.IMCOLLECT)) = 'CONTRACTOR''S CHOICE'
),

AvailableInventory AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN COALESCE(IM.IMFACT, 0) = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN DECIMAL(IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0), 18, 5)
                ELSE DECIMAL((IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0)) * IM.IMFACT, 18, 5)
            END
        ) AS TOTAL_SF_AVAILABLE
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON TRIM(IM.IMITEM) = TRIM(IB.IBITEM)
    JOIN ReportItems RI
      ON RI.ITEM_NUMBER = TRIM(IB.IBITEM)
    WHERE IB.IBCO = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY
        TRIM(IB.IBITEM)
),

SalesPast2Years AS (
    SELECT
        TRIM(L.SLITEM) AS ITEM_NUMBER,
        SUM(COALESCE(L.SLBLUO, 0)) AS TOTAL_SALES_SF_2YR
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H
      ON H.SHCO = L.SLCO
     AND H.SHLOC = L.SLLOC
     AND H.SHORD# = L.SLORD#
     AND H.SHINV# = L.SLINV#
    JOIN ReportItems RI
      ON RI.ITEM_NUMBER = TRIM(L.SLITEM)
    WHERE H.SHIDAT >= (CURRENT_DATE - 2 YEARS)
      AND COALESCE(L.SLBLUO, 0) > 0
      AND H.SHCUST NOT LIKE '%TRANSFER%'
      AND H.SHCUST NOT LIKE '%OMP000%'
      AND H.SHCUST NOT LIKE '%INV000%'
      AND H.SHCUST NOT LIKE '%OLD001%'
    GROUP BY
        TRIM(L.SLITEM)
)

SELECT
    RI.ITEM_NUMBER AS "Item Number",
    RI.DESCRIPTION AS "Description",
    RI.SALE_PRICE AS "Sale Price",
    DECIMAL(COALESCE(AI.TOTAL_SF_AVAILABLE, 0), 18, 2) AS "Total SF Available",
    DECIMAL(COALESCE(S2.TOTAL_SALES_SF_2YR, 0), 18, 2) AS "Total Sales SF Past 2 Years"
FROM ReportItems RI
LEFT JOIN AvailableInventory AI
  ON AI.ITEM_NUMBER = RI.ITEM_NUMBER
LEFT JOIN SalesPast2Years S2
  ON S2.ITEM_NUMBER = RI.ITEM_NUMBER
ORDER BY
    RI.ITEM_NUMBER
