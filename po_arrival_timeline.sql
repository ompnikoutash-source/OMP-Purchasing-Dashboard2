/* ============================================================
   PO ARRIVAL TIMELINE — CHRONOLOGICAL VIEW
   DB: DB2/AS400 via ODBC, schema GSFL2K

   One row per open PO line, sorted by ETW date.

   Columns:
     ETW                    POLINE.PLDDAT  (due-in-inventory / warehouse date)
     Item Number            POLINE.PLITEM
     Description            ITEMMAST.IMDESC
     Collection             ITEMXTRA.IMCOLLECT
     PO Number              POLINE.PLPO#
     Physical Inv Available (IBQOH - IBQAL) * IMFACT  companywide, locs 1/3/4/5/6/8/9/51
     Uncommitted on PO      (PLBLUO - PLBLUR) minus item-level attached BOs (OLPOR='Y')
     Unattached Backorders  Sum of open BO lines not yet attached to any PO (OLPOR<>'Y')

   How BO attachment works in this system (confirmed via diagnostics):
     OOLINE.OLPOR = 'Y'  means the backorder line has been attached to an incoming PO.
     OOLINE.OLPOR = 'P'  means the backorder line is pending / not yet attached.
     OOLINE.OLBLUB       is the backorder quantity in billing/sales UOM (SF for flooring).
     POLINE stores no per-PO commitment counter (PLQBO and PLBLUB are not updated
     by BO attachments), so attachment totals are sourced from OOLINE directly.

   "Uncommitted on PO" is item-level, not per-specific-PO:
     The database does not record WHICH PO each attached BO belongs to.  All
     attached BOs for an item are subtracted from each open PO line's open qty.
     For items with one open PO this is exact.  For items with multiple open POs
     the same attached total appears on every PO row — consistent with how the
     Gartman P/O-B/O Inquiry screen calculates "Not Attached."

   UOM notes:
     PLBLUO / PLBLUR  are in billing/sales UOM  (SF for flooring items).
     OLBLUB           is in billing/sales UOM (SF).
     IBQOH / IBQAL    are in stocking UOM; IMFACT converts to SF.

   "Open" PO line: PLDELT <> 'D'  AND  PLQORD > PLQREC.
   Transfers and RA orders excluded from all BO counts.
   ============================================================ */

WITH

/* ── 1. Physical inventory available, companywide ───────────────────
   (IBQOH - IBQAL) = on hand minus what is already allocated to pick
   tickets.  IMFACT converts stocking UOM → sales UOM (SF).         */
PhysicalAvail AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                    THEN CASE WHEN (IB.IBQOH - IB.IBQAL) > 0
                              THEN (IB.IBQOH - IB.IBQAL) ELSE 0 END
                ELSE CASE WHEN (IB.IBQOH - IB.IBQAL) > 0
                          THEN (IB.IBQOH - IB.IBQAL) ELSE 0 END * IM.IMFACT
            END
        ) AS AVAIL_SF
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
        ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO  = 1
      AND IB.IBLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY TRIM(IB.IBITEM)
),

/* ── 2. Backorder status from OOLINE, per item ──────────────────────
   Scans all open backorder lines (OLQBO > 0), splits by OLPOR flag:
     ATTACHED_BO_SF   = SF committed to an incoming PO  (OLPOR = 'Y')
     UNATTACHED_BO_SF = SF not yet linked to any PO     (OLPOR <> 'Y')
   Transfers and RA order types are excluded.                        */
BOStatus AS (
    SELECT
        TRIM(L.OLITEM) AS ITEM_NUMBER,
        SUM(
            CASE WHEN COALESCE(TRIM(L.OLPOR), '') = 'Y'
                 THEN COALESCE(L.OLBLUB, 0) ELSE 0 END
        ) AS ATTACHED_BO_SF,
        SUM(
            CASE WHEN COALESCE(TRIM(L.OLPOR), '') <> 'Y'
                 THEN COALESCE(L.OLBLUB, 0) ELSE 0 END
        ) AS UNATTACHED_BO_SF
    FROM GSFL2K.OOLINE L
    JOIN GSFL2K.OOHEAD H
        ON H.OHCO   = L.OLCO
       AND H.OHLOC  = L.OLLOC
       AND H.OHORD# = L.OLORD#
    WHERE L.OLCO  = 1
      AND L.OLQBO > 0
      AND TRIM(H.OHCUST) NOT LIKE '%TRANSFER%'
      AND H.OHOTYP NOT LIKE '%RA%'
    GROUP BY TRIM(L.OLITEM)
)

/* ── 3. Main result: one row per open PO line ───────────────────── */
SELECT
    PL.PLDDAT                                        AS "ETW",
    TRIM(PL.PLITEM)                                  AS "Item Number",
    TRIM(IM.IMDESC)                                  AS "Description",
    TRIM(IX.IMCOLLECT)                               AS "Collection",
    TRIM(CHAR(PL.PLPO#))                             AS "PO Number",

    /* Physical inventory on hand and not yet allocated to a pick ticket */
    DECIMAL(COALESCE(PA.AVAIL_SF, 0), 18, 2)        AS "Physical Inv Available",

    /* Open PO qty (SF) minus item-level BOs already attached to an incoming PO.
       Clamped to 0.  See header note on why this is item-level.           */
    DECIMAL(
        GREATEST(0,
            COALESCE(PL.PLBLUO, 0)
          - COALESCE(PL.PLBLUR, 0)
          - COALESCE(BO.ATTACHED_BO_SF, 0)
        ), 18, 2
    )                                                AS "Uncommitted on PO",

    /* Open backorder SF for this item that has NOT been attached to any PO */
    DECIMAL(COALESCE(BO.UNATTACHED_BO_SF, 0), 18, 2) AS "Unattached Backorders"

FROM GSFL2K.POLINE PL

JOIN GSFL2K.ITEMMAST IM
    ON IM.IMITEM = PL.PLITEM

JOIN GSFL2K.ITEMXTRA IX
    ON IX.IMXITM = PL.PLITEM

LEFT JOIN PhysicalAvail PA
    ON PA.ITEM_NUMBER = TRIM(PL.PLITEM)

LEFT JOIN BOStatus BO
    ON BO.ITEM_NUMBER = TRIM(PL.PLITEM)

WHERE PL.PLCO  = 1
  AND COALESCE(TRIM(PL.PLDELT), '') <> 'D'    -- exclude deleted lines
  AND PL.PLQORD > COALESCE(PL.PLQREC, 0)      -- qty remaining to receive > 0
  AND PL.PLDDAT IS NOT NULL                    -- must have an ETW date

ORDER BY
    PL.PLDDAT      ASC,
    TRIM(PL.PLITEM),
    TRIM(CHAR(PL.PLPO#))

FOR READ ONLY