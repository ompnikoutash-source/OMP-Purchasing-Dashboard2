/* ============================================================
   VENDOR 299 ORDERS WITH PROMO299 OFFSET LINE
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Purpose:
     Lists billed invoice lines and currently open order lines where the
     same order/invoice contains both:
       1. At least one non-PROMO299 item whose ITEMMAST vendor is 299
       2. A PROMO299 line used to offset/promo out the price

   Output:
     One row per relevant line on those orders:
       - Vendor 299 item line
       - PROMO299 offset line

     Order-level totals are repeated on each row so the vendor item amount
     can be compared against the PROMO299 offset amount.
   ============================================================ */

WITH

ItemInfo AS (
    SELECT
        TRIM(IM.IMITEM) AS ITEM_NUMBER,
        TRIM(IM.IMDESC) AS DESCRIPTION,
        IM.IMDIV AS DIVISION,
        IM.IMVEND AS VENDOR_NUMBER,
        DECIMAL(COALESCE(IM.IMP1, 0), 18, 5) AS CURRENT_SALE_PRICE
    FROM GSFL2K.ITEMMAST IM
),

BilledPromoOrders AS (
    SELECT
        L.SLCO AS COMPANY,
        L.SLLOC AS ORDER_LOCATION,
        L.SLORD# AS ORDER_NUMBER_RAW,
        L.SLINV# AS INVOICE_NUMBER_RAW,
        SUM(
            CASE
                WHEN II.VENDOR_NUMBER = 299
                 AND II.ITEM_NUMBER <> 'PROMO299'
                    THEN 1
                ELSE 0
            END
        ) AS VENDOR_299_ITEM_LINES,
        SUM(
            CASE
                WHEN II.ITEM_NUMBER = 'PROMO299'
                    THEN 1
                ELSE 0
            END
        ) AS PROMO299_LINES,
        DECIMAL(SUM(
            CASE
                WHEN II.VENDOR_NUMBER = 299
                 AND II.ITEM_NUMBER <> 'PROMO299'
                    THEN COALESCE(L.SLENET, 0)
                ELSE 0
            END
        ), 18, 2) AS ORDER_VENDOR_299_AMOUNT,
        DECIMAL(SUM(
            CASE
                WHEN II.ITEM_NUMBER = 'PROMO299'
                    THEN COALESCE(L.SLENET, 0)
                ELSE 0
            END
        ), 18, 2) AS ORDER_PROMO299_AMOUNT
    FROM GSFL2K.SHLINE L
    JOIN ItemInfo II
      ON II.ITEM_NUMBER = TRIM(L.SLITEM)
    GROUP BY
        L.SLCO,
        L.SLLOC,
        L.SLORD#,
        L.SLINV#
    HAVING SUM(
            CASE
                WHEN II.VENDOR_NUMBER = 299
                 AND II.ITEM_NUMBER <> 'PROMO299'
                    THEN 1
                ELSE 0
            END
        ) > 0
       AND SUM(
            CASE
                WHEN II.ITEM_NUMBER = 'PROMO299'
                    THEN 1
                ELSE 0
            END
        ) > 0
),

OpenPromoOrders AS (
    SELECT
        L.OLCO AS COMPANY,
        L.OLLOC AS ORDER_LOCATION,
        L.OLORD# AS ORDER_NUMBER_RAW,
        SUM(
            CASE
                WHEN II.VENDOR_NUMBER = 299
                 AND II.ITEM_NUMBER <> 'PROMO299'
                    THEN 1
                ELSE 0
            END
        ) AS VENDOR_299_ITEM_LINES,
        SUM(
            CASE
                WHEN II.ITEM_NUMBER = 'PROMO299'
                    THEN 1
                ELSE 0
            END
        ) AS PROMO299_LINES,
        DECIMAL(SUM(
            CASE
                WHEN II.VENDOR_NUMBER = 299
                 AND II.ITEM_NUMBER <> 'PROMO299'
                    THEN COALESCE(L.OLENET, 0)
                ELSE 0
            END
        ), 18, 2) AS ORDER_VENDOR_299_AMOUNT,
        DECIMAL(SUM(
            CASE
                WHEN II.ITEM_NUMBER = 'PROMO299'
                    THEN COALESCE(L.OLENET, 0)
                ELSE 0
            END
        ), 18, 2) AS ORDER_PROMO299_AMOUNT
    FROM GSFL2K.OOLINE L
    JOIN ItemInfo II
      ON II.ITEM_NUMBER = TRIM(L.OLITEM)
    GROUP BY
        L.OLCO,
        L.OLLOC,
        L.OLORD#
    HAVING SUM(
            CASE
                WHEN II.VENDOR_NUMBER = 299
                 AND II.ITEM_NUMBER <> 'PROMO299'
                    THEN 1
                ELSE 0
            END
        ) > 0
       AND SUM(
            CASE
                WHEN II.ITEM_NUMBER = 'PROMO299'
                    THEN 1
                ELSE 0
            END
        ) > 0
),

BilledLines AS (
    SELECT
        'BILLED' AS ORDER_STATUS,
        H.SHCO AS COMPANY,
        H.SHLOC AS ORDER_LOCATION,
        TRIM(CHAR(H.SHORD#)) AS ORDER_NUMBER,
        TRIM(H.SHINV#) AS INVOICE_NUMBER,
        H.SHIDAT AS TRANSACTION_DATE,
        TRIM(H.SHCUST) AS ACCOUNT_NUMBER,
        TRIM(C.CMNAME) AS CUSTOMER_NAME,
        TRIM(H.SHOTYP) AS ORDER_TYPE,
        L.SLSEQ# AS LINE_NUMBER,
        CASE
            WHEN II.ITEM_NUMBER = 'PROMO299'
                THEN 'PROMO299 offset line'
            ELSE 'Vendor 299 item line'
        END AS LINE_ROLE,
        II.ITEM_NUMBER,
        II.DESCRIPTION,
        II.DIVISION,
        TRIM(CHAR(II.VENDOR_NUMBER)) AS VENDOR_NUMBER,
        TRIM(VM.VMNAME) AS VENDOR_NAME,
        DECIMAL(II.CURRENT_SALE_PRICE, 18, 5) AS CURRENT_ITEM_SALE_PRICE,
        DECIMAL(COALESCE(L.SLPRIC, 0), 18, 5) AS ENTERED_UNIT_PRICE,
        DECIMAL(COALESCE(L.SLNETPRICE, 0), 18, 5) AS NET_UNIT_PRICE,
        DECIMAL(COALESCE(L.SLORIGPRIC, 0), 18, 5) AS ORIGINAL_UNIT_PRICE,
        DECIMAL(COALESCE(L.SLENET, 0), 18, 2) AS LINE_NET_AMOUNT,
        DECIMAL(COALESCE(L.SLBLUO, 0), 18, 5) AS QUANTITY,
        TRIM(L.SLUM2) AS UOM,
        BPO.VENDOR_299_ITEM_LINES,
        BPO.PROMO299_LINES,
        BPO.ORDER_VENDOR_299_AMOUNT,
        BPO.ORDER_PROMO299_AMOUNT,
        DECIMAL(BPO.ORDER_VENDOR_299_AMOUNT + BPO.ORDER_PROMO299_AMOUNT, 18, 2) AS ORDER_NET_AFTER_PROMO,
        TRIM(S.SMNAME) AS SALESPERSON,
        TRIM(L.SLEUSER) AS ENTERED_BY
    FROM GSFL2K.SHLINE L
    JOIN GSFL2K.SHHEAD H
      ON H.SHCO = L.SLCO
     AND H.SHLOC = L.SLLOC
     AND H.SHORD# = L.SLORD#
     AND H.SHINV# = L.SLINV#
    JOIN BilledPromoOrders BPO
      ON BPO.COMPANY = L.SLCO
     AND BPO.ORDER_LOCATION = L.SLLOC
     AND BPO.ORDER_NUMBER_RAW = L.SLORD#
     AND BPO.INVOICE_NUMBER_RAW = L.SLINV#
    JOIN ItemInfo II
      ON II.ITEM_NUMBER = TRIM(L.SLITEM)
    LEFT JOIN GSFL2K.CUSTMAST C
      ON C.CMCO = H.SHCO
     AND TRIM(C.CMCUST) = TRIM(H.SHCUST)
    LEFT JOIN GSFL2K.VENDMAST VM
      ON VM.VMVEND = II.VENDOR_NUMBER
    LEFT JOIN GSFL2K.SALESMAN S
      ON S.SMNO = H.SHSLSM
    WHERE II.VENDOR_NUMBER = 299
       OR II.ITEM_NUMBER = 'PROMO299'
),

OpenLines AS (
    SELECT
        'OPEN' AS ORDER_STATUS,
        H.OHCO AS COMPANY,
        H.OHLOC AS ORDER_LOCATION,
        TRIM(CHAR(H.OHORD#)) AS ORDER_NUMBER,
        CAST('' AS CHAR(6)) AS INVOICE_NUMBER,
        L.OLDATE AS TRANSACTION_DATE,
        TRIM(H.OHCUST) AS ACCOUNT_NUMBER,
        TRIM(C.CMNAME) AS CUSTOMER_NAME,
        TRIM(H.OHOTYP) AS ORDER_TYPE,
        L.OLSEQ# AS LINE_NUMBER,
        CASE
            WHEN II.ITEM_NUMBER = 'PROMO299'
                THEN 'PROMO299 offset line'
            ELSE 'Vendor 299 item line'
        END AS LINE_ROLE,
        II.ITEM_NUMBER,
        II.DESCRIPTION,
        II.DIVISION,
        TRIM(CHAR(II.VENDOR_NUMBER)) AS VENDOR_NUMBER,
        TRIM(VM.VMNAME) AS VENDOR_NAME,
        DECIMAL(II.CURRENT_SALE_PRICE, 18, 5) AS CURRENT_ITEM_SALE_PRICE,
        DECIMAL(COALESCE(L.OLPRIC, 0), 18, 5) AS ENTERED_UNIT_PRICE,
        DECIMAL(COALESCE(L.OLNETPRICE, 0), 18, 5) AS NET_UNIT_PRICE,
        DECIMAL(COALESCE(L.OLORIGPRIC, 0), 18, 5) AS ORIGINAL_UNIT_PRICE,
        DECIMAL(COALESCE(L.OLENET, 0), 18, 2) AS LINE_NET_AMOUNT,
        DECIMAL(COALESCE(L.OLBLUO, 0), 18, 5) AS QUANTITY,
        TRIM(L.OLUM2) AS UOM,
        OPO.VENDOR_299_ITEM_LINES,
        OPO.PROMO299_LINES,
        OPO.ORDER_VENDOR_299_AMOUNT,
        OPO.ORDER_PROMO299_AMOUNT,
        DECIMAL(OPO.ORDER_VENDOR_299_AMOUNT + OPO.ORDER_PROMO299_AMOUNT, 18, 2) AS ORDER_NET_AFTER_PROMO,
        TRIM(S.SMNAME) AS SALESPERSON,
        TRIM(L.OLEUSER) AS ENTERED_BY
    FROM GSFL2K.OOLINE L
    JOIN GSFL2K.OOHEAD H
      ON H.OHCO = L.OLCO
     AND H.OHLOC = L.OLLOC
     AND H.OHORD# = L.OLORD#
    JOIN OpenPromoOrders OPO
      ON OPO.COMPANY = L.OLCO
     AND OPO.ORDER_LOCATION = L.OLLOC
     AND OPO.ORDER_NUMBER_RAW = L.OLORD#
    JOIN ItemInfo II
      ON II.ITEM_NUMBER = TRIM(L.OLITEM)
    LEFT JOIN GSFL2K.CUSTMAST C
      ON C.CMCO = H.OHCO
     AND TRIM(C.CMCUST) = TRIM(H.OHCUST)
    LEFT JOIN GSFL2K.VENDMAST VM
      ON VM.VMVEND = II.VENDOR_NUMBER
    LEFT JOIN GSFL2K.SALESMAN S
      ON S.SMNO = L.OLSLMN
    WHERE II.VENDOR_NUMBER = 299
       OR II.ITEM_NUMBER = 'PROMO299'
)

SELECT
    ORDER_STATUS AS "Order Status",
    COMPANY AS "Company",
    ORDER_LOCATION AS "Order Location",
    ORDER_NUMBER AS "Order Number",
    INVOICE_NUMBER AS "Invoice Number",
    TRANSACTION_DATE AS "Transaction Date",
    ACCOUNT_NUMBER AS "Account Number",
    CUSTOMER_NAME AS "Customer Name",
    ORDER_TYPE AS "Order Type",
    LINE_NUMBER AS "Line Number",
    LINE_ROLE AS "Line Role",
    ITEM_NUMBER AS "Item Number",
    DESCRIPTION AS "Description",
    DIVISION AS "Division",
    VENDOR_NUMBER AS "Vendor Number",
    VENDOR_NAME AS "Vendor Name",
    CURRENT_ITEM_SALE_PRICE AS "Current Item Sale Price",
    ENTERED_UNIT_PRICE AS "Entered Unit Price",
    NET_UNIT_PRICE AS "Net Unit Price",
    ORIGINAL_UNIT_PRICE AS "Original Unit Price",
    LINE_NET_AMOUNT AS "Line Net Amount",
    QUANTITY AS "Quantity",
    UOM AS "UOM",
    VENDOR_299_ITEM_LINES AS "Vendor 299 Item Lines On Order",
    PROMO299_LINES AS "PROMO299 Lines On Order",
    ORDER_VENDOR_299_AMOUNT AS "Order Vendor 299 Amount",
    ORDER_PROMO299_AMOUNT AS "Order PROMO299 Amount",
    ORDER_NET_AFTER_PROMO AS "Order Net After Promo",
    SALESPERSON AS "Salesperson",
    ENTERED_BY AS "Entered By"
FROM BilledLines

UNION ALL

SELECT
    ORDER_STATUS AS "Order Status",
    COMPANY AS "Company",
    ORDER_LOCATION AS "Order Location",
    ORDER_NUMBER AS "Order Number",
    INVOICE_NUMBER AS "Invoice Number",
    TRANSACTION_DATE AS "Transaction Date",
    ACCOUNT_NUMBER AS "Account Number",
    CUSTOMER_NAME AS "Customer Name",
    ORDER_TYPE AS "Order Type",
    LINE_NUMBER AS "Line Number",
    LINE_ROLE AS "Line Role",
    ITEM_NUMBER AS "Item Number",
    DESCRIPTION AS "Description",
    DIVISION AS "Division",
    VENDOR_NUMBER AS "Vendor Number",
    VENDOR_NAME AS "Vendor Name",
    CURRENT_ITEM_SALE_PRICE AS "Current Item Sale Price",
    ENTERED_UNIT_PRICE AS "Entered Unit Price",
    NET_UNIT_PRICE AS "Net Unit Price",
    ORIGINAL_UNIT_PRICE AS "Original Unit Price",
    LINE_NET_AMOUNT AS "Line Net Amount",
    QUANTITY AS "Quantity",
    UOM AS "UOM",
    VENDOR_299_ITEM_LINES AS "Vendor 299 Item Lines On Order",
    PROMO299_LINES AS "PROMO299 Lines On Order",
    ORDER_VENDOR_299_AMOUNT AS "Order Vendor 299 Amount",
    ORDER_PROMO299_AMOUNT AS "Order PROMO299 Amount",
    ORDER_NET_AFTER_PROMO AS "Order Net After Promo",
    SALESPERSON AS "Salesperson",
    ENTERED_BY AS "Entered By"
FROM OpenLines

ORDER BY
    "Order Status",
    "Transaction Date" DESC,
    "Order Number",
    "Line Number"
