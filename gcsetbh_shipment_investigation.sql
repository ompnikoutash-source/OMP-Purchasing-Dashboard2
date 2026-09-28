/* ============================================================
   GCSETBH-2828 SAMPLE SET SHIPMENT INVESTIGATION
   DB: DB2/AS400 via ODBC, schema GSFL2K

   QUERY A — Sales history (SHLINE/SHHEAD)
     Who received these items and when. Only lot number (SLDYLT)
     is available at the sales line level — serial and tag are
     not stored on SHLINE.

   QUERY B — Current inventory detail (ITEMDETL)
     Tag number (IDKY) and serial (IDSERL) for any units still
     physically in stock. Use this to identify which tags from
     the shipment have NOT yet been shipped out.

   NOTE: Serial and tag tracking only exist on ITEMDETL (inventory
   receipt records). Neither SLSERL nor SLKY are columns on SHLINE.
   ============================================================ */


/* ============================================================
   QUERY A — Sales history
   One row per invoice line for all three GCSETBH-2828 SKUs.
   ============================================================ */

SELECT
    TRIM(CHAR(H.SHORD#))   AS ORDER_NUMBER,
    TRIM(L.SLITEM)          AS ITEM_NUMBER,
    TRIM(H.SHCUST)          AS ACCOUNT_NUMBER,
    TRIM(S.SMNAME)          AS OUTSIDE_REP,
    TRIM(L.SLEUSER)         AS INSIDE_REP,
    H.SHIDAT                AS INVOICE_DATE,
    L.SLBLUO                AS QTY_UNITS,
    TRIM(L.SLUM2)           AS UOM,
    TRIM(L.SLDYLT)          AS LOT_NUM
FROM GSFL2K.SHLINE L
JOIN GSFL2K.SHHEAD H
    ON  H.SHCO   = L.SLCO
    AND H.SHLOC  = L.SLLOC
    AND H.SHORD# = L.SLORD#
    AND H.SHINV# = L.SLINV#
LEFT JOIN GSFL2K.SALESMAN S
    ON  S.SMNO = H.SHSLSM
WHERE TRIM(L.SLITEM) IN (
    'GCSETBH-2828',
    'GCSETBH-2828ADD',
    'GCSETBH-2828PL'
)
ORDER BY
    H.SHIDAT        DESC,
    TRIM(H.SHCUST),
    TRIM(L.SLITEM)


/* ============================================================
   QUERY B — Current inventory detail with tag and serial
   Shows every on-hand receipt tag for the three SKUs.
   Units with IDQOH > 0 are still physically in stock.
   ============================================================ */

SELECT
    TRIM(D.IDITEM)          AS ITEM_NUMBER,
    D.IDLOC                 AS LOCATION,
    TRIM(D.IDKY)            AS TAG_NUM,
    TRIM(D.IDSERL)          AS SERIAL_NUM,
    D.IDDATE                AS RECEIPT_DATE,
    D.IDQOH                 AS QTY_ON_HAND,
    D.IDQOH - D.IDQAL       AS QTY_AVAILABLE,
    D.IDQAL                 AS QTY_COMMITTED,
    D.IDCOST                AS UNIT_COST
FROM GSFL2K.ITEMDETL D
WHERE TRIM(D.IDITEM) IN (
    'GCSETBH-2828',
    'GCSETBH-2828ADD',
    'GCSETBH-2828PL'
)
ORDER BY
    TRIM(D.IDITEM),
    D.IDDATE        DESC,
    D.IDLOC


/* ============================================================
   QUERY C — Inventory transaction log discovery
   Searches QSYS2.SYSTABLES for tables whose names suggest
   movement history (TRAN, HIST, LEDG, MOVE, ISSUE, LOG).
   Run this to find if a tag-level movement audit trail exists.
   If a matching table appears, paste its name into the
   discovery SELECT below to explore its columns.
   ============================================================ */

SELECT
    TABLE_NAME,
    TABLE_SCHEMA,
    TABLE_TEXT
FROM QSYS2.SYSTABLES
WHERE TABLE_SCHEMA = 'GSFL2K'
  AND (   TABLE_NAME LIKE 'ITEM%'
       OR TABLE_NAME LIKE 'INV%'
       OR TABLE_NAME LIKE 'IMTRAN%'
       OR TABLE_NAME LIKE 'ITMTRAN%'
      )
ORDER BY TABLE_NAME


/* ============================================================
   QUERY D — Full tag-to-order trace via ITEMRCHSOX
   ITEMRCHSOX (Item receipts history by sales order line) links
   each inventory tag to the order/customer it was shipped on.
   Key fields confirmed: IRKY (tag), IRSERL (serial), IRDYLT (lot),
   IRORD# (order), IRCUST (customer), IRINV# (invoice),
   IRIDAT (invoice date), IRSHP# (shipment/manifest number).
   ============================================================ */

SELECT
    TRIM(R.IRITEM)          AS ITEM_NUMBER,
    TRIM(R.IRKY)            AS TAG_NUM,
    TRIM(R.IRSERL)          AS SERIAL_NUM,
    TRIM(R.IRDYLT)          AS LOT_NUM,
    TRIM(CHAR(R."IRORD#"))  AS ORDER_NUMBER,
    TRIM(R.IRCUST)          AS ACCOUNT_NUMBER,
    TRIM(R."IRINV#")        AS INVOICE_NUMBER,
    R.IRIDAT                AS INVOICE_DATE,
    TRIM(R."IRSHP#")        AS SHIPMENT_NUMBER,
    R.IRDATE                AS TRANSACTION_DATE,
    R.IRQTY                 AS QTY,
    TRIM(R.IRUM1)           AS UOM,
    TRIM(R.IRUSER)          AS ENTERED_BY
FROM GSFL2K.ITEMRCHSOX R
WHERE TRIM(R.IRITEM) IN (
    'GCSETBH-2828',
    'GCSETBH-2828ADD',
    'GCSETBH-2828PL'
)
ORDER BY
    R.IRIDAT                DESC,
    TRIM(R.IRITEM),
    TRIM(R.IRKY)


/* ============================================================
   QUERY E — Problematic shipment trace: serial 60349
   Shows all outbound movements from the 60349 shipment.
   Negative QTY = shipped to customer or transferred out.
   Positive QTY = receipt or correction.
   Use ACCOUNT_NUMBER to build the replacement contact list.
   ============================================================ */

SELECT
    TRIM(R.IRITEM)          AS ITEM_NUMBER,
    TRIM(R.IRKY)            AS TAG_NUM,
    TRIM(R.IRSERL)          AS SERIAL_NUM,
    TRIM(CHAR(R."IRORD#"))  AS ORDER_NUMBER,
    TRIM(R.IRCUST)          AS ACCOUNT_NUMBER,
    TRIM(C.CMNAME)          AS CUSTOMER_NAME,
    TRIM(S.SMNAME)          AS OUTSIDE_REP,
    R.IRDATE                AS TRANSACTION_DATE,
    R.IRQTY                 AS QTY
FROM GSFL2K.ITEMRCHSOX R
LEFT JOIN GSFL2K.CUSTMAST C
    ON TRIM(C.CMCUST) = TRIM(R.IRCUST)
LEFT JOIN GSFL2K.SALESMAN S
    ON S.SMNO = C.CMSLMN
WHERE TRIM(R.IRITEM) IN (
    'GCSETBH-2828',
    'GCSETBH-2828ADD',
    'GCSETBH-2828PL'
)
  AND TRIM(R.IRSERL) LIKE '60349%'
  AND R.IRQTY < 0
  AND TRIM(R.IRCUST) NOT LIKE 'TRANSFER%'
ORDER BY
    R.IRDATE                ASC,
    TRIM(R.IRCUST)
