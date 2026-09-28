/* ============================================================
   SALES BY LOCATION — TRAILING 36 MONTHS
   Generated for report date: 2026-05-05
   ─────────────────────────────────────────────────────────────
   Date range: trailing 36 months from current date
   Locations:  1, 3, 4, 5, 6, 8, 9
   One row per customer account. Customers with zero sales in the
   window are excluded.
   DB: DB2/AS400, schema GSFL2K
   ============================================================ */

WITH AGG AS (
    SELECT
        TRIM(H.SHCUST)                                                               AS CUSTOMER_NUMBER,
        SUM(CASE WHEN H.SHLOC = 1 THEN COALESCE(L.SLENET, 0) ELSE 0 END)            AS SALES_LOC_1,
        SUM(CASE WHEN H.SHLOC = 3 THEN COALESCE(L.SLENET, 0) ELSE 0 END)            AS SALES_LOC_3,
        SUM(CASE WHEN H.SHLOC = 4 THEN COALESCE(L.SLENET, 0) ELSE 0 END)            AS SALES_LOC_4,
        SUM(CASE WHEN H.SHLOC = 5 THEN COALESCE(L.SLENET, 0) ELSE 0 END)            AS SALES_LOC_5,
        SUM(CASE WHEN H.SHLOC = 6 THEN COALESCE(L.SLENET, 0) ELSE 0 END)            AS SALES_LOC_6,
        SUM(CASE WHEN H.SHLOC = 8 THEN COALESCE(L.SLENET, 0) ELSE 0 END)            AS SALES_LOC_8,
        SUM(CASE WHEN H.SHLOC = 9 THEN COALESCE(L.SLENET, 0) ELSE 0 END)            AS SALES_LOC_9
    FROM GSFL2K.SHHEAD H
    JOIN GSFL2K.SHLINE L
        ON  L.SLCO   = H.SHCO
        AND L.SLLOC  = H.SHLOC
        AND L.SLINV# = H.SHINV#
        AND L.SLORD# = H.SHORD#
    WHERE H.SHIDAT >= ADD_MONTHS(CURRENT_DATE, -36)
    GROUP BY TRIM(H.SHCUST)
)

SELECT
    TRIM(C.CMCUST)                AS ACCOUNT_NUMBER,
    COALESCE(A.SALES_LOC_1, 0)   AS SALES_LOC_1,
    COALESCE(A.SALES_LOC_3, 0)   AS SALES_LOC_3,
    COALESCE(A.SALES_LOC_4, 0)   AS SALES_LOC_4,
    COALESCE(A.SALES_LOC_5, 0)   AS SALES_LOC_5,
    COALESCE(A.SALES_LOC_6, 0)   AS SALES_LOC_6,
    COALESCE(A.SALES_LOC_8, 0)   AS SALES_LOC_8,
    COALESCE(A.SALES_LOC_9, 0)   AS SALES_LOC_9
FROM GSFL2K.CUSTMAST C
INNER JOIN AGG A
    ON A.CUSTOMER_NUMBER = TRIM(C.CMCUST)
ORDER BY TRIM(C.CMCUST)
FOR READ ONLY
