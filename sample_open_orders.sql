/* ============================================================
   SAMPLE OPEN ORDERS REPORT
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Returns one row per open order line for all Division 5 items.
   Mirrors the Sample Invoice query structure with DATE_ENTERED
   in place of INVOICE_DATE.

   Columns:
     ORDER_NUMBER     -- H.OHORD#
     ITEM_NUMBER      -- L.OLITEM
     DESCRIPTION      -- M.IMDESC   (item description from ITEMMAST)
     ACCOUNT_NUMBER   -- H.OHCUST
     OUTSIDE_REP      -- S.SMNAME   (salesman name from SALESMAN)
     INSIDE_REP       -- L.OLEUSER  (user who entered the order line)
     DATE_ENTERED     -- L.OLDATE   (line entry date on OOLINE)
     QTY_UNITS        -- L.OLBLUO   (open quantity in billing UOM)
     UOM              -- L.OLUM2    (billing unit of measure)

   RA-type orders and TRANSFER accounts are excluded.
   ============================================================ */

SELECT
    TRIM(CHAR(H.OHORD#))    AS ORDER_NUMBER,
    TRIM(L.OLITEM)          AS ITEM_NUMBER,
    TRIM(M.IMDESC)          AS DESCRIPTION,
    TRIM(H.OHCUST)          AS ACCOUNT_NUMBER,
    TRIM(S.SMNAME)          AS OUTSIDE_REP,
    TRIM(L.OLEUSER)         AS INSIDE_REP,
    L.OLDATE                AS DATE_ENTERED,
    L.OLBLUO                AS QTY_UNITS,
    TRIM(L.OLUM2)           AS UOM
FROM GSFL2K.OOLINE L
JOIN GSFL2K.OOHEAD H
    ON  H.OHCO   = L.OLCO
    AND H.OHLOC  = L.OLLOC
    AND H.OHORD# = L.OLORD#
JOIN GSFL2K.ITEMMAST M
    ON  TRIM(M.IMITEM) = TRIM(L.OLITEM)
    AND M.IMDIV = 5
LEFT JOIN GSFL2K.SALESMAN S
    ON  S.SMNO = L.OLSLMN
WHERE H.OHOTYP NOT LIKE '%RA%'
  AND TRIM(H.OHCUST) NOT LIKE '%TRANSFER%'
ORDER BY
    L.OLDATE        DESC,
    TRIM(H.OHCUST),
    TRIM(L.OLITEM)
