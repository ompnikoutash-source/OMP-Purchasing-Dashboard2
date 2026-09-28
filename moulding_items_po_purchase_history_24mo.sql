/* ============================================================
   SELECT MOULDING ITEMS - PO PURCHASE HISTORY, PAST 24 MONTHS
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Purpose:
     One row per purchase receipt/tag for the screenshot item list,
     received in the past 24 months.

   Screenshot corrections found in ITEMMAST:
     - MCOCAMD120MUL -> MOCAMD120MUL
     - MORIM751      -> MORIMD751
     - MOOQR34PR     -> MOQR34PR

   Quantity basis:
     - "Quantity On PO" uses ITEMRECH.IRBLU when populated; otherwise it
       converts ITEMRECH.IRQTY to sales/billing UOM using IRFACT.
     - "Quantity Still In Inventory Of That PO" uses live ITEMDETL.IDQOH
       matched back to the receipt by receipt record number, PO fields,
       tag/serial/lot, and cost where available.
     - This is physical on-hand, not available inventory.

   Landed cost basis:
     - ITEMRECH.IRCOST is treated as landed cost per sales/billing UOM.
   ============================================================ */

WITH

ReportItems (ITEM_NUMBER) AS (
    VALUES
        ('MOCAMD120MUL'),
        ('MOBBMD314MUL3'),
        ('MOBBMD314MUL4'),
        ('MOBBMD314MUL5'),
        ('MOBBMD318MUL'),
        ('MOBBMD324MUL'),
        ('MOBBMD329MUL'),
        ('MOBBMD330MUL'),
        ('MOBBMD342MUL'),
        ('MOBBMD387MUL3'),
        ('MOBBMD387MUL4'),
        ('MOBBMD387MUL5'),
        ('MORIMD751'),
        ('MOBSPR300BFJ'),
        ('MOQR34PR')
),

ItemInfo AS (
    SELECT
        TRIM(IM.IMITEM) AS ITEM_NUMBER,
        TRIM(IM.IMDESC) AS DESCRIPTION,
        IM.IMDIV AS DIVISION,
        DECIMAL(COALESCE(IM.IMP1, 0), 18, 5) AS IMP1,
        IM.IMVEND AS ITEM_VENDOR_NUMBER,
        TRIM(IM.IMUM1) AS STOCKING_UOM,
        TRIM(IM.IMUM2) AS SALES_UOM,
        COALESCE(IM.IMFACT, 0) AS UOM_FACTOR
    FROM GSFL2K.ITEMMAST IM
    JOIN ReportItems RI
      ON RI.ITEM_NUMBER = TRIM(IM.IMITEM)
),

ReceiptLines AS (
    SELECT
        II.ITEM_NUMBER,
        II.DESCRIPTION,
        II.DIVISION,
        II.IMP1,
        II.STOCKING_UOM,
        II.SALES_UOM,
        II.UOM_FACTOR,
        R.IRPOCO AS PO_COMPANY,
        R.IRPOLO AS PO_LOCATION,
        R.IRPO# AS PO_NUMBER,
        R.IRPORL AS PO_RELEASE,
        R.IRPOSQ AS PO_SEQUENCE,
        R.IRVEND AS VENDOR_NUMBER,
        R.IRRECNBR AS RECEIPT_RECORD_NUMBER,
        R.IRDATE AS RECEIVED_DATE,
        TRIM(R.IRKY) AS TAG_NUMBER,
        TRIM(R.IRSERL) AS SERIAL_NUMBER,
        TRIM(R.IRDYLT) AS LOT_NUMBER,
        CASE
            WHEN COALESCE(TRIM(R.IRKY), '') <> ''
             AND COALESCE(TRIM(R.IRSERL), '') <> ''
             AND COALESCE(TRIM(R.IRDYLT), '') <> ''
                THEN TRIM(R.IRKY) || ' / ' || TRIM(R.IRSERL) || ' / ' || TRIM(R.IRDYLT)
            WHEN COALESCE(TRIM(R.IRKY), '') <> ''
             AND COALESCE(TRIM(R.IRSERL), '') <> ''
                THEN TRIM(R.IRKY) || ' / ' || TRIM(R.IRSERL)
            WHEN COALESCE(TRIM(R.IRKY), '') <> ''
                THEN TRIM(R.IRKY)
            WHEN COALESCE(TRIM(R.IRSERL), '') <> ''
                THEN TRIM(R.IRSERL)
            WHEN COALESCE(TRIM(R.IRDYLT), '') <> ''
                THEN TRIM(R.IRDYLT)
            ELSE ''
        END AS SERIAL_TAG_LOT_NUMBER,
        CASE
            WHEN COALESCE(R.IRBLU, 0) <> 0 THEN DECIMAL(R.IRBLU, 18, 5)
            WHEN COALESCE(R.IRFACT, 0) = 0 OR TRIM(R.IRUM1) = TRIM(R.IRUM2)
                THEN DECIMAL(R.IRQTY, 18, 5)
            ELSE DECIMAL(R.IRQTY * R.IRFACT, 18, 5)
        END AS RECEIVED_QTY_SALES_UOM,
        DECIMAL(R.IRCOST, 18, 5) AS LANDED_COST
    FROM ItemInfo II
    JOIN GSFL2K.ITEMRECH R
      ON TRIM(R.IRITEM) = II.ITEM_NUMBER
    WHERE R.IRCO = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRQTY > 0
      AND R.IRRECNBR > 0
      AND R.IRDATE >= ADD_MONTHS(CURRENT_DATE, -24)
      AND R.IRCOST > 0
),

CurrentInventoryFromReceipt AS (
    SELECT
        RL.RECEIPT_RECORD_NUMBER,
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
    JOIN ReceiptLines RL
      ON RL.ITEM_NUMBER = TRIM(D.IDITEM)
     AND RL.VENDOR_NUMBER = D.IDVEND
     AND (
          (
              COALESCE(D.IDRECNBR, 0) > 0
              AND D.IDRECNBR = RL.RECEIPT_RECORD_NUMBER
          )
          OR
          (
              COALESCE(D.IDPO#, 0) > 0
              AND D.IDPOCO = RL.PO_COMPANY
              AND D.IDPOLO = RL.PO_LOCATION
              AND D.IDPO# = RL.PO_NUMBER
              AND D.IDPORL = RL.PO_RELEASE
              AND D.IDPOSQ = RL.PO_SEQUENCE
              AND (
                    COALESCE(TRIM(D.IDKY), '') = RL.TAG_NUMBER
                 OR COALESCE(TRIM(D.IDKY), '') = ''
                 OR RL.TAG_NUMBER = ''
              )
          )
          OR
          (
              COALESCE(D.IDRECNBR, 0) = 0
              AND COALESCE(D.IDPO#, 0) = 0
              AND COALESCE(TRIM(D.IDSERL), '') <> ''
              AND COALESCE(TRIM(D.IDSERL), '') = RL.SERIAL_NUMBER
              AND (
                    COALESCE(TRIM(D.IDKY), '') = RL.TAG_NUMBER
                 OR COALESCE(TRIM(D.IDKY), '') = ''
                 OR RL.TAG_NUMBER = ''
              )
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
    GROUP BY
        RL.RECEIPT_RECORD_NUMBER
),

ReceiptAgg AS (
    SELECT
        RL.ITEM_NUMBER,
        RL.DESCRIPTION,
        RL.DIVISION,
        RL.IMP1,
        RL.PO_NUMBER,
        RL.PO_COMPANY,
        RL.PO_LOCATION,
        RL.PO_RELEASE,
        RL.PO_SEQUENCE,
        RL.VENDOR_NUMBER,
        RL.RECEIVED_DATE,
        RL.SERIAL_TAG_LOT_NUMBER,
        RL.SALES_UOM,
        SUM(RL.RECEIVED_QTY_SALES_UOM) AS QUANTITY_ON_PO,
        SUM(COALESCE(CI.QTY_STILL_IN_INVENTORY, 0)) AS QTY_STILL_IN_INVENTORY,
        DECIMAL(
            SUM(RL.LANDED_COST * RL.RECEIVED_QTY_SALES_UOM)
            / NULLIF(SUM(RL.RECEIVED_QTY_SALES_UOM), 0),
            18,
            5
        ) AS LANDED_COST_OF_PO
    FROM ReceiptLines RL
    LEFT JOIN CurrentInventoryFromReceipt CI
      ON CI.RECEIPT_RECORD_NUMBER = RL.RECEIPT_RECORD_NUMBER
    GROUP BY
        RL.ITEM_NUMBER,
        RL.DESCRIPTION,
        RL.DIVISION,
        RL.IMP1,
        RL.PO_NUMBER,
        RL.PO_COMPANY,
        RL.PO_LOCATION,
        RL.PO_RELEASE,
        RL.PO_SEQUENCE,
        RL.VENDOR_NUMBER,
        RL.RECEIVED_DATE,
        RL.SERIAL_TAG_LOT_NUMBER,
        RL.SALES_UOM
)

SELECT
    RA.ITEM_NUMBER AS "Item Number",
    RA.DESCRIPTION AS "Description",
    RA.DIVISION AS "Division",
    RA.IMP1 AS "IMP1",
    TRIM(CHAR(RA.VENDOR_NUMBER)) AS "Vendor Number",
    TRIM(VM.VMNAME) AS "Vendor Name",
    TRIM(CHAR(RA.PO_NUMBER)) AS "PO#",
    RA.SERIAL_TAG_LOT_NUMBER AS "Serial/Tag/Lot Number",
    DECIMAL(RA.QUANTITY_ON_PO, 18, 2) AS "Quantity On That PO",
    DECIMAL(RA.QTY_STILL_IN_INVENTORY, 18, 2) AS "Quantity Still In Inventory Of That PO",
    RA.LANDED_COST_OF_PO AS "Landed Cost Of That PO",
    RA.RECEIVED_DATE AS "Receiving Date"
FROM ReceiptAgg RA
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = RA.VENDOR_NUMBER
ORDER BY
    RA.ITEM_NUMBER,
    RA.RECEIVED_DATE DESC,
    RA.PO_NUMBER DESC,
    RA.PO_SEQUENCE
