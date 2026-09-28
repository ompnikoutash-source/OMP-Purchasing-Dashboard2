/* ============================================================
   OPEN PO REPORT — VENDOR 560
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Returns one row per open PO line for vendor 560.
   "Open" = line not deleted and qty remaining to receive > 0.

   Columns:
     ITEM_NUMBER      -- PL.PLITEM
     PO_NUMBER        -- PL.PLPO#
     DATE_TO_PORT     -- PL.PLSHIPDATE  (Ship Date)
     DATE_DUE_IN_INV  -- PL.PLDDAT      (Due Date)
   ============================================================ */

SELECT
    TRIM(PL.PLITEM)        AS "Item Number",
    TRIM(CHAR(PL.PLPO#))   AS "PO Number",
    PL.PLSHIPDATE          AS "Date to Port",
    PL.PLDDAT              AS "Date Due in Inventory"
FROM GSFL2K.POLINE PL
WHERE PL.PLCO   = 1
  AND PL.PLVEND = 560
  AND COALESCE(TRIM(PL.PLDELT), '') <> 'D'
  AND PL.PLQORD > COALESCE(PL.PLQREC, 0)
ORDER BY
    PL.PLSHIPDATE  ASC,
    TRIM(PL.PLITEM)
