/* ============================================================
   INVENTORY RECEIPTS — PAST 365 DAYS
   DB: DB2/AS400 via ODBC, schema GSFL2K

   One row per PO receipt line for items physically received
   within the past 365 days, across all locations.

   Source: ITEMRECH with IRSRC = 'P' (purchase receipts only).
   IRDATE  = actual arrival/receipt date (not a "last touched" date).
   IRCOST  = FOB cost per billing unit at time of receipt.
   IRBLU   = quantity received in billing unit (e.g. SF).

   NOTE: The previous version used ITEMDETL.IDDATE which is a
   "last modified" date — it updates on counts, transfers, and
   adjustments, NOT only on receipts. That caused old inventory
   to appear with recent dates and stale costs.
   ============================================================ */

SELECT
    TRIM(IM.IMITEM)              AS "Item Number",
    TRIM(IM.IMDESC)              AS "Description",
    TRIM(IM.IMCOLR)              AS "Collection",
    IM.IMDIV                     AS "Division",
    IR.IRVEND                    AS "Vendor Number",
    TRIM(VM.VMNAME)              AS "Vendor Name",
    TRIM(IR.IRSERL)              AS "Serial",
    IR.IRDATE                    AS "Date Received",
    IR.IREDAT                    AS "Date Entered",
    IR.IRBLU                     AS "Qty Received",
    TRIM(IR.IRUM2)               AS "UOM",
    DECIMAL(IR.IRCOST,    10, 4) AS "Unit Cost",
    DECIMAL(IM.IMP1,      10, 4) AS "Sell Price",
    CASE
        WHEN IR.IRCOST > 0 AND IM.IMP1 > 0
        THEN DECIMAL((IM.IMP1 - IR.IRCOST) / IM.IMP1, 8, 4)
        ELSE NULL
    END                          AS "Margin"
FROM GSFL2K.ITEMRECH IR
JOIN GSFL2K.ITEMMAST IM
    ON  TRIM(IR.IRITEM) = TRIM(IM.IMITEM)
LEFT JOIN GSFL2K.VENDMAST VM
    ON  IR.IRVEND = VM.VMVEND
WHERE IR.IRSRC = 'P'
  AND IR.IRDATE >= CURRENT_DATE - 365 DAYS
ORDER BY
    IR.IRDATE       DESC,
    TRIM(IM.IMITEM),
    TRIM(IR.IRSERL)
