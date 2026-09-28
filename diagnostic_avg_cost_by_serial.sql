/* ============================================================
   DIAGNOSTIC — Avg Inventory Cost by Serial/Tag
   For a single item: shows each on-hand tag's cost, qty,
   and its weighted contribution to the average.

   Change the IDITEM filter at the bottom to spot-check any item.
   ============================================================ */

SELECT
    TRIM(D.IDITEM)                          AS "Item Number",
    TRIM(D.IDSERL)                          AS "Serial / Tag",
    D.IDLOC                                 AS "Location",
    DECIMAL(D.IDQOH,   18, 4)               AS "Qty On Hand (stk UOM)",
    DECIMAL(D.IDCOST,  18, 4)               AS "Cost (per stk UOM)",

    -- Raw dollar value for this tag: cost × qty
    DECIMAL(D.IDCOST * D.IDQOH, 18, 4)      AS "Tag Dollar Value",

    -- Qty converted to sales UOM (what the denominator uses)
    DECIMAL(
        CASE
            WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                THEN D.IDQOH
            ELSE D.IDQOH * IM.IMFACT
        END,
        18, 4
    )                                        AS "Qty (sales UOM)",

    TRIM(IM.IMUM1)                          AS "Stk UOM",
    TRIM(IM.IMUM2)                          AS "Sales UOM",
    DECIMAL(IM.IMFACT, 18, 6)               AS "Conv Factor (IMFACT)",

    -- Per-tag implied cost in sales UOM (cost / IMFACT when units differ)
    DECIMAL(
        CASE
            WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
                THEN D.IDCOST
            ELSE D.IDCOST / IM.IMFACT
        END,
        18, 4
    )                                        AS "Cost (per sales UOM)",

    COALESCE(D.IDDELT, '')                  AS "Delete Flag",
    D.IDDATE                                AS "Last Modified Date"

FROM GSFL2K.ITEMDETL D
JOIN GSFL2K.ITEMMAST IM
  ON TRIM(IM.IMITEM) = TRIM(D.IDITEM)

WHERE TRIM(D.IDITEM) = 'AAFL026-5'
  AND D.IDCO = 1

ORDER BY D.IDLOC, D.IDQOH DESC
