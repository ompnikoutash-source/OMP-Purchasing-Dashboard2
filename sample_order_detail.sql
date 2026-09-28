/* ============================================================
   SAMPLE ORDER DETAIL REPORT
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Returns one row per line item for all invoice lines
   belonging to Division 5 items.

   Columns:
     ORDER_NUMBER     -- H.SHORD#   (order number)
     ITEM_NUMBER      -- L.SLITEM
     DESCRIPTION      -- M.IMDESC   (item description from ITEMMAST)
     ACCOUNT_NUMBER   -- H.SHCUST
     OUTSIDE_REP      -- S.SMNAME   (salesman name from SALESMAN)
     INSIDE_REP       -- L.SLEUSER  (user who entered the order line)
     INVOICE_DATE     -- H.SHIDAT
     QTY_UNITS        -- L.SLBLUO   (billed quantity in billing UOM)
     UOM              -- L.SLUM2    (billing unit of measure)
   ============================================================ */

SELECT
    TRIM(CHAR(H.SHORD#))   AS ORDER_NUMBER,
    TRIM(L.SLITEM)          AS ITEM_NUMBER,
    TRIM(M.IMDESC)          AS DESCRIPTION,
    TRIM(H.SHCUST)          AS ACCOUNT_NUMBER,
    TRIM(S.SMNAME)          AS OUTSIDE_REP,
    TRIM(L.SLEUSER)         AS INSIDE_REP,
    H.SHIDAT                AS INVOICE_DATE,
    L.SLBLUO                AS QTY_UNITS,
    TRIM(L.SLUM2)           AS UOM
FROM GSFL2K.SHLINE L
JOIN GSFL2K.SHHEAD H
    ON  H.SHCO   = L.SLCO
    AND H.SHLOC  = L.SLLOC
    AND H.SHORD# = L.SLORD#
    AND H.SHINV# = L.SLINV#
JOIN GSFL2K.ITEMMAST M
    ON  TRIM(M.IMITEM) = TRIM(L.SLITEM)
    AND M.IMDIV = 5
LEFT JOIN GSFL2K.SALESMAN S
    ON  S.SMNO = H.SHSLSM
ORDER BY
    H.SHIDAT        DESC,
    TRIM(H.SHCUST),
    TRIM(L.SLITEM)
