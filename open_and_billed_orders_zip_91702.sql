/* ============================================================
   OPEN + BILLED ORDERS WITH DELIVERY ZIP STARTING 91702
   DB: DB2/AS400 via ODBC, schema GSFL2K

   NOTE: OOHEAD's ship-to zip column is assumed to be OHZIP, by analogy
   with SHHEAD.SHZIP (confirmed elsewhere in this repo). Verify OHZIP
   actually exists on OOHEAD before running -- if the file has a
   different column name for ship-to zip, swap it in below.

   Zip columns are stored as packed numbers that may include a +4
   suffix (e.g. 917021234), so we divide by 10000 to get the 5-digit
   zip before comparing to 91702.
   ============================================================ */

WITH

OpenOrders AS (
    SELECT
        'OPEN' AS ORDER_STATUS,
        H.OHCO AS COMPANY,
        H.OHLOC AS LOCATION,
        TRIM(CHAR(H.OHORD#)) AS ORDER_NUMBER,
        CAST('' AS CHAR(6)) AS INVOICE_NUMBER,
        MIN(L.OLDATE) AS ORDER_DATE,
        TRIM(H.OHCUST) AS ACCOUNT_NUMBER,
        TRIM(C.CMNAME) AS CUSTOMER_NAME,
        TRIM(H.OHOTYP) AS ORDER_TYPE,
        CASE
            WHEN COALESCE(H.OHZIP, 0) >= 100000000 THEN INTEGER(H.OHZIP / 10000)
            WHEN COALESCE(H.OHZIP, 0) BETWEEN 10000 AND 99999 THEN INTEGER(H.OHZIP)
            ELSE NULL
        END AS SHIP_ZIP5,
        TRIM(S.SMNAME) AS SALESPERSON,
        DECIMAL(SUM(COALESCE(L.OLENET, 0)), 18, 2) AS ORDER_NET_AMOUNT,
        DECIMAL(SUM(CASE WHEN TRIM(L.OLUM2) = 'SF' THEN COALESCE(L.OLBLUO, 0) ELSE 0 END), 18, 2) AS TOTAL_SF
    FROM GSFL2K.OOHEAD H
    JOIN GSFL2K.OOLINE L
      ON L.OLCO = H.OHCO
     AND L.OLLOC = H.OHLOC
     AND L.OLORD# = H.OHORD#
    LEFT JOIN GSFL2K.CUSTMAST C
      ON C.CMCO = H.OHCO
     AND TRIM(C.CMCUST) = TRIM(H.OHCUST)
    LEFT JOIN GSFL2K.SALESMAN S
      ON S.SMNO = L.OLSLMN
    GROUP BY
        H.OHCO,
        H.OHLOC,
        H.OHORD#,
        TRIM(H.OHCUST),
        TRIM(C.CMNAME),
        TRIM(H.OHOTYP),
        H.OHZIP,
        TRIM(S.SMNAME)
    HAVING CASE
            WHEN COALESCE(H.OHZIP, 0) >= 100000000 THEN INTEGER(H.OHZIP / 10000)
            WHEN COALESCE(H.OHZIP, 0) BETWEEN 10000 AND 99999 THEN INTEGER(H.OHZIP)
            ELSE NULL
        END = 91702
),

BilledOrders AS (
    SELECT
        'BILLED' AS ORDER_STATUS,
        H.SHCO AS COMPANY,
        H.SHLOC AS LOCATION,
        TRIM(CHAR(H.SHORD#)) AS ORDER_NUMBER,
        TRIM(H.SHINV#) AS INVOICE_NUMBER,
        H.SHIDAT AS ORDER_DATE,
        TRIM(H.SHCUST) AS ACCOUNT_NUMBER,
        TRIM(C.CMNAME) AS CUSTOMER_NAME,
        TRIM(H.SHOTYP) AS ORDER_TYPE,
        CASE
            WHEN COALESCE(H.SHZIP, 0) >= 100000000 THEN INTEGER(H.SHZIP / 10000)
            WHEN COALESCE(H.SHZIP, 0) BETWEEN 10000 AND 99999 THEN INTEGER(H.SHZIP)
            ELSE NULL
        END AS SHIP_ZIP5,
        TRIM(S.SMNAME) AS SALESPERSON,
        DECIMAL(SUM(COALESCE(L.SLENET, 0)), 18, 2) AS ORDER_NET_AMOUNT,
        DECIMAL(SUM(CASE WHEN TRIM(L.SLUM2) = 'SF' THEN COALESCE(L.SLBLUO, 0) ELSE 0 END), 18, 2) AS TOTAL_SF
    FROM GSFL2K.SHHEAD H
    JOIN GSFL2K.SHLINE L
      ON L.SLCO = H.SHCO
     AND L.SLLOC = H.SHLOC
     AND L.SLORD# = H.SHORD#
     AND L.SLINV# = H.SHINV#
    LEFT JOIN GSFL2K.CUSTMAST C
      ON C.CMCO = H.SHCO
     AND TRIM(C.CMCUST) = TRIM(H.SHCUST)
    LEFT JOIN GSFL2K.SALESMAN S
      ON S.SMNO = H.SHSLSM
    GROUP BY
        H.SHCO,
        H.SHLOC,
        H.SHORD#,
        TRIM(H.SHINV#),
        H.SHIDAT,
        TRIM(H.SHCUST),
        TRIM(C.CMNAME),
        TRIM(H.SHOTYP),
        H.SHZIP,
        TRIM(S.SMNAME)
    HAVING CASE
            WHEN COALESCE(H.SHZIP, 0) >= 100000000 THEN INTEGER(H.SHZIP / 10000)
            WHEN COALESCE(H.SHZIP, 0) BETWEEN 10000 AND 99999 THEN INTEGER(H.SHZIP)
            ELSE NULL
        END = 91702
)

SELECT
    ORDER_STATUS AS "Order Status",
    COMPANY AS "Company",
    LOCATION AS "Location",
    ORDER_NUMBER AS "Order Number",
    INVOICE_NUMBER AS "Invoice Number",
    ORDER_DATE AS "Order Date",
    ACCOUNT_NUMBER AS "Account Number",
    CUSTOMER_NAME AS "Customer Name",
    ORDER_TYPE AS "Order Type",
    SHIP_ZIP5 AS "Delivery Zip",
    SALESPERSON AS "Salesperson",
    ORDER_NET_AMOUNT AS "Order Net Amount",
    TOTAL_SF AS "Total SF"
FROM OpenOrders

UNION ALL

SELECT
    ORDER_STATUS AS "Order Status",
    COMPANY AS "Company",
    LOCATION AS "Location",
    ORDER_NUMBER AS "Order Number",
    INVOICE_NUMBER AS "Invoice Number",
    ORDER_DATE AS "Order Date",
    ACCOUNT_NUMBER AS "Account Number",
    CUSTOMER_NAME AS "Customer Name",
    ORDER_TYPE AS "Order Type",
    SHIP_ZIP5 AS "Delivery Zip",
    SALESPERSON AS "Salesperson",
    ORDER_NET_AMOUNT AS "Order Net Amount",
    TOTAL_SF AS "Total SF"
FROM BilledOrders

ORDER BY
    "Order Status",
    "Order Date" DESC,
    "Order Number"
