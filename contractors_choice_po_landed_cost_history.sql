/* ============================================================
   CONTRACTOR'S CHOICE - PO LANDED COST HISTORY
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Purpose:
     One row per Contractor's Choice item / PO line receipt history
     for purchase receipts in the past 2 years.

   Quantity basis:
     - Received quantity uses ITEMRECH.IRBLU when available. IRBLU is the
       billing/sales UOM quantity, matching ITEMMAST.IMP1 sale price and
       ITEMRECH.IRCOST landed cost basis used in the other cost reports.
     - Still-in-inventory quantity uses ITEMDETL.IDQOH, converted from
       stocking UOM to sales UOM when needed. Live ITEMDETL rows often have
       IDPO# / IDPOCO / IDPOLO / IDPORL / IDPOSQ set to zero after transfers
       or splits, so the current inventory is traced back to the PO receipt by:
         1) IDRECNBR = IRRECNBR when available,
         2) PO fields when ITEMDETL keeps them,
         3) item/vendor/serial/lot/cost fallback for child tags.
     - "Still in inventory" is physical on-hand from that PO, not available.
       To report available instead, change IDQOH to (IDQOH - IDQAL) in the
       CurrentInventoryFromPO CTE.

   Landed cost basis:
     - ITEMRECH.IRCOST is treated as landed cost per sales UOM.
     - When an item/PO line has multiple receipt rows, the report returns
       a received-quantity-weighted landed cost for that PO line.
   ============================================================ */

WITH

ReportItems AS (
    SELECT
        TRIM(IM.IMITEM)    AS ITEM_NUMBER,
        TRIM(IM.IMDESC)    AS DESCRIPTION,
        DECIMAL(IM.IMP1, 18, 5) AS SALE_PRICE,
        IM.IMDIV           AS DIVISION,
        TRIM(IX.IMCOLLECT) AS COLLECTION,
        TRIM(IM.IMUM1)     AS STOCKING_UOM,
        TRIM(IM.IMUM2)     AS SALES_UOM,
        COALESCE(IM.IMFACT, 0) AS UOM_FACTOR
    FROM GSFL2K.ITEMMAST IM
    JOIN GSFL2K.ITEMXTRA IX
      ON IX.IMXITM = IM.IMITEM
    WHERE UPPER(TRIM(IX.IMCOLLECT)) = 'CONTRACTOR''S CHOICE'
),

ReceiptLines AS (
    SELECT
        RI.ITEM_NUMBER,
        RI.DESCRIPTION,
        RI.SALE_PRICE,
        RI.DIVISION,
        RI.COLLECTION,
        RI.STOCKING_UOM,
        RI.SALES_UOM,
        R.IRPOCO AS PO_COMPANY,
        R.IRPOLO AS PO_LOCATION,
        R.IRPO#  AS PO_NUMBER,
        R.IRPORL AS PO_RELEASE,
        R.IRPOSQ AS PO_SEQUENCE,
        R.IRVEND AS VENDOR_NUMBER,
        R.IRRECNBR AS RECEIPT_RECORD_NUMBER,
        TRIM(R.IRKY) AS RECEIPT_TAG_NUMBER,
        TRIM(R.IRSERL) AS SERIAL_NUMBER,
        TRIM(R.IRDYLT) AS LOT_NUMBER,
        R.IRDATE AS RECEIVED_DATE,
        CASE
            WHEN COALESCE(R.IRBLU, 0) <> 0 THEN DECIMAL(R.IRBLU, 18, 5)
            WHEN COALESCE(R.IRFACT, 0) = 0 OR TRIM(R.IRUM1) = TRIM(R.IRUM2)
                THEN DECIMAL(R.IRQTY, 18, 5)
            ELSE DECIMAL(R.IRQTY * R.IRFACT, 18, 5)
        END AS RECEIVED_QTY_SALES_UOM,
        DECIMAL(R.IRCOST, 18, 5) AS LANDED_COST
    FROM ReportItems RI
    JOIN GSFL2K.ITEMRECH R
      ON TRIM(R.IRITEM) = RI.ITEM_NUMBER
    WHERE R.IRCO = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRQTY > 0
      AND R.IRRECNBR > 0
      AND R.IRDATE >= (CURRENT_DATE - 2 YEARS)
      AND R.IRCOST > 0
),

ReceiptAgg AS (
    SELECT
        ITEM_NUMBER,
        DESCRIPTION,
        SALE_PRICE,
        DIVISION,
        COLLECTION,
        SALES_UOM,
        PO_COMPANY,
        PO_LOCATION,
        PO_NUMBER,
        PO_RELEASE,
        PO_SEQUENCE,
        VENDOR_NUMBER,
        MIN(RECEIVED_DATE) AS FIRST_RECEIVED_DATE,
        MAX(RECEIVED_DATE) AS DATE_RECEIVED_IN_INVENTORY,
        SUM(RECEIVED_QTY_SALES_UOM) AS FULL_PO_QTY_RECEIVED,
        DECIMAL(
            SUM(LANDED_COST * RECEIVED_QTY_SALES_UOM)
            / NULLIF(SUM(RECEIVED_QTY_SALES_UOM), 0),
            18,
            5
        ) AS LANDED_COST_FOR_PO
    FROM ReceiptLines
    GROUP BY
        ITEM_NUMBER,
        DESCRIPTION,
        SALE_PRICE,
        DIVISION,
        COLLECTION,
        SALES_UOM,
        PO_COMPANY,
        PO_LOCATION,
        PO_NUMBER,
        PO_RELEASE,
        PO_SEQUENCE,
        VENDOR_NUMBER
),

CurrentInventoryFromPO AS (
    SELECT
        RL.ITEM_NUMBER,
        RL.VENDOR_NUMBER,
        RL.PO_COMPANY,
        RL.PO_LOCATION,
        RL.PO_NUMBER,
        RL.PO_RELEASE,
        RL.PO_SEQUENCE,
        SUM(
            CASE
                WHEN COALESCE(IM.IMFACT, 0) = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN DECIMAL(D.IDQOH, 18, 5)
                ELSE DECIMAL(D.IDQOH * IM.IMFACT, 18, 5)
            END
        ) AS QTY_STILL_IN_INVENTORY
    FROM GSFL2K.ITEMDETL D
    JOIN GSFL2K.ITEMMAST IM
      ON TRIM(IM.IMITEM) = TRIM(D.IDITEM)
    JOIN GSFL2K.ITEMXTRA IX
      ON IX.IMXITM = IM.IMITEM
    JOIN ReceiptLines RL
      ON RL.ITEM_NUMBER = TRIM(D.IDITEM)
     AND RL.VENDOR_NUMBER = D.IDVEND
     AND (
          (COALESCE(D.IDRECNBR, 0) > 0 AND D.IDRECNBR = RL.RECEIPT_RECORD_NUMBER)
          OR
          (
              COALESCE(D.IDPO#, 0) > 0
              AND D.IDPOCO = RL.PO_COMPANY
              AND D.IDPOLO = RL.PO_LOCATION
              AND D.IDPO#  = RL.PO_NUMBER
              AND D.IDPORL = RL.PO_RELEASE
              AND D.IDPOSQ = RL.PO_SEQUENCE
          )
          OR
          (
              COALESCE(D.IDRECNBR, 0) = 0
              AND COALESCE(D.IDPO#, 0) = 0
              AND COALESCE(TRIM(D.IDSERL), '') <> ''
              AND COALESCE(TRIM(D.IDSERL), '') = RL.SERIAL_NUMBER
              AND (
                    COALESCE(TRIM(D.IDDYLT), '') = RL.LOT_NUMBER
                    OR COALESCE(TRIM(D.IDDYLT), '') = ''
                    OR RL.LOT_NUMBER = ''
                  )
              AND DECIMAL(D.IDCOST, 18, 5) = RL.LANDED_COST
          )
      )
    WHERE D.IDCO = 1
      AND COALESCE(D.IDDELT, '') <> 'D'
      AND D.IDQOH > 0
      AND UPPER(TRIM(IX.IMCOLLECT)) = 'CONTRACTOR''S CHOICE'
    GROUP BY
        RL.ITEM_NUMBER,
        RL.VENDOR_NUMBER,
        RL.PO_COMPANY,
        RL.PO_LOCATION,
        RL.PO_NUMBER,
        RL.PO_RELEASE,
        RL.PO_SEQUENCE
)

SELECT
    RA.ITEM_NUMBER AS "Item Number",
    RA.DESCRIPTION AS "Description",
    RA.SALE_PRICE AS "Sale Price",
    RA.DIVISION AS "Division",
    TRIM(CHAR(RA.PO_NUMBER)) AS "PO Number",
    (
        TRIM(CHAR(RA.PO_COMPANY)) || '-' ||
        TRIM(CHAR(RA.PO_LOCATION)) || '-' ||
        TRIM(CHAR(RA.PO_NUMBER)) || '-' ||
        TRIM(CHAR(RA.PO_RELEASE)) || '-' ||
        TRIM(CHAR(RA.PO_SEQUENCE))
    ) AS "PO Line Key",
    TRIM(CHAR(RA.VENDOR_NUMBER)) AS "Vendor Number",
    TRIM(VM.VMNAME) AS "Vendor Name",
    RA.DATE_RECEIVED_IN_INVENTORY AS "Date Received In Inventory",
    RA.FIRST_RECEIVED_DATE AS "First Received Date",
    RA.SALES_UOM AS "UOM",
    DECIMAL(RA.FULL_PO_QTY_RECEIVED, 18, 2) AS "Full PO Qty Received",
    DECIMAL(COALESCE(CI.QTY_STILL_IN_INVENTORY, 0), 18, 2) AS "Qty From PO Still In Inventory",
    RA.LANDED_COST_FOR_PO AS "Landed Cost For PO"
FROM ReceiptAgg RA
LEFT JOIN CurrentInventoryFromPO CI
  ON CI.ITEM_NUMBER   = RA.ITEM_NUMBER
 AND CI.VENDOR_NUMBER = RA.VENDOR_NUMBER
 AND CI.PO_COMPANY    = RA.PO_COMPANY
 AND CI.PO_LOCATION   = RA.PO_LOCATION
 AND CI.PO_NUMBER     = RA.PO_NUMBER
 AND CI.PO_RELEASE    = RA.PO_RELEASE
 AND CI.PO_SEQUENCE   = RA.PO_SEQUENCE
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = RA.VENDOR_NUMBER
ORDER BY
    RA.ITEM_NUMBER,
    RA.DATE_RECEIVED_IN_INVENTORY DESC,
    RA.PO_NUMBER DESC,
    RA.PO_SEQUENCE
