/* ============================================================
   REPORT CODE 806 - 7' MOLDING VS MATCHED 8' MOLDING PRICE CHECK
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Filters:
     - Includes items where report code 806 is in ITEMMAST.IMRPT1 or IMRPT2.
     - Includes only items with positive current available inventory at
       Gardena / Location 4.

   Matching logic:
     - First tries replacing the length marker after the 4-character molding
       prefix with 8. Example: GCNO7WH00BS -> GCNO8WH00BS.
     - If that is not found, tries removing the 7. Example:
       MOTM7FO -> MOTMFO, MORE7FM -> MOREFM, MONO7HO -> MONOHO.
     - Rows without an item-number match are flagged for manual review.
   ============================================================ */

WITH
ReportItems AS (
    SELECT
        TRIM(IM.IMITEM) AS ITEM_NUMBER,
        TRIM(IM.IMDESC) AS DESCRIPTION,
        CAST(COALESCE(IM.IMP1, 0) AS DECIMAL(18, 5)) AS SALE_IMP1
    FROM GSFL2K.ITEMMAST IM
    WHERE 806 IN (IM.IMRPT1, IM.IMRPT2)
),
Loc4Inventory AS (
    SELECT
        TRIM(IB.IBITEM) AS ITEM_NUMBER,
        SUM(
            CASE
                WHEN IM.IMFACT = 0 OR TRIM(IM.IMUM1) = TRIM(IM.IMUM2)
                    THEN (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0))
                ELSE (IB.IBQOH - IB.IBQOO - COALESCE(IB.IBQAL, 0)) * IM.IMFACT
            END
        ) AS LOC4_AVAIL
    FROM GSFL2K.ITEMBAL IB
    JOIN GSFL2K.ITEMMAST IM
      ON IM.IMITEM = IB.IBITEM
    WHERE IB.IBCO = 1
      AND IB.IBLOC = 4
    GROUP BY TRIM(IB.IBITEM)
),
LatestLandedCost AS (
    SELECT
        TRIM(R.IRITEM) AS ITEM_NUMBER,
        CAST(MAX(R.IRCOST) AS DECIMAL(18, 5)) AS COST_LANDED
    FROM GSFL2K.ITEMRECH R
    WHERE R.IRCO = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRQTY > 0
      AND R.IRCOST > 0
      AND R.IRRECNBR > 0
      AND R.IRRECNBR = (
          SELECT MAX(R2.IRRECNBR)
          FROM GSFL2K.ITEMRECH R2
          WHERE TRIM(R2.IRITEM) = TRIM(R.IRITEM)
            AND R2.IRCO = 1
            AND TRIM(R2.IRSRC) = 'P'
            AND R2.IRQTY > 0
            AND R2.IRCOST > 0
            AND R2.IRRECNBR > 0
      )
    GROUP BY TRIM(R.IRITEM)
),
Candidates AS (
    SELECT
        RI.ITEM_NUMBER AS ITEM_7,
        SUBSTR(RI.ITEM_NUMBER, 1, 4) || '8' || SUBSTR(RI.ITEM_NUMBER, 6) AS REPLACE_7_WITH_8_CANDIDATE,
        SUBSTR(RI.ITEM_NUMBER, 1, 4) || SUBSTR(RI.ITEM_NUMBER, 6) AS REMOVE_7_CANDIDATE
    FROM ReportItems RI
),
Matched8 AS (
    SELECT
        C.ITEM_7,
        CASE
            WHEN I8_REPLACE.IMITEM IS NOT NULL THEN TRIM(I8_REPLACE.IMITEM)
            WHEN I8_REMOVE.IMITEM IS NOT NULL THEN TRIM(I8_REMOVE.IMITEM)
            ELSE NULL
        END AS ITEM_8,
        CASE
            WHEN I8_REPLACE.IMITEM IS NOT NULL THEN 'replace 7 with 8'
            WHEN I8_REMOVE.IMITEM IS NOT NULL THEN 'remove 7'
            ELSE 'manual match needed'
        END AS MATCH_METHOD,
        CASE
            WHEN I8_REPLACE.IMITEM IS NOT NULL THEN TRIM(I8_REPLACE.IMDESC)
            WHEN I8_REMOVE.IMITEM IS NOT NULL THEN TRIM(I8_REMOVE.IMDESC)
            ELSE NULL
        END AS DESCRIPTION_8,
        CAST(COALESCE(
            CASE
                WHEN I8_REPLACE.IMITEM IS NOT NULL THEN I8_REPLACE.IMP1
                WHEN I8_REMOVE.IMITEM IS NOT NULL THEN I8_REMOVE.IMP1
                ELSE NULL
            END,
            0
        ) AS DECIMAL(18, 5)) AS SALE_8
    FROM Candidates C
    LEFT JOIN GSFL2K.ITEMMAST I8_REPLACE
      ON TRIM(I8_REPLACE.IMITEM) = C.REPLACE_7_WITH_8_CANDIDATE
    LEFT JOIN GSFL2K.ITEMMAST I8_REMOVE
      ON TRIM(I8_REMOVE.IMITEM) = C.REMOVE_7_CANDIDATE
),
Base AS (
    SELECT
        RI.ITEM_NUMBER AS ITEM_7,
        RI.DESCRIPTION AS DESCRIPTION_7,
        CAST(COALESCE(L4.LOC4_AVAIL, 0) AS DECIMAL(18, 2)) AS LOC4_AVAIL_7,
        CAST(COALESCE(LC7.COST_LANDED, 0) AS DECIMAL(18, 5)) AS COST_7,
        RI.SALE_IMP1 AS SALE_7,
        M.ITEM_8,
        M.DESCRIPTION_8,
        M.MATCH_METHOD,
        CAST(COALESCE(LC8.COST_LANDED, 0) AS DECIMAL(18, 5)) AS COST_8,
        M.SALE_8
    FROM ReportItems RI
    LEFT JOIN Loc4Inventory L4
      ON L4.ITEM_NUMBER = RI.ITEM_NUMBER
    LEFT JOIN LatestLandedCost LC7
      ON LC7.ITEM_NUMBER = RI.ITEM_NUMBER
    LEFT JOIN Matched8 M
      ON M.ITEM_7 = RI.ITEM_NUMBER
    LEFT JOIN LatestLandedCost LC8
      ON LC8.ITEM_NUMBER = M.ITEM_8
    WHERE COALESCE(L4.LOC4_AVAIL, 0) > 0
),
Pricing AS (
    SELECT
        B.*,
        CASE WHEN B.SALE_8 > 0 THEN CAST(B.SALE_8 * 7 / 8 AS DECIMAL(18, 2)) ELSE NULL END AS SALE_8_NORMALIZED_TO_7,
        CASE WHEN B.SALE_8 > 0 THEN CAST(B.SALE_8 * 7 / 8 * 0.90 AS DECIMAL(18, 2)) ELSE NULL END AS SUGGESTED_10_BELOW,
        CASE WHEN B.SALE_8 > 0 THEN CAST(B.SALE_8 * 7 / 8 * 0.85 AS DECIMAL(18, 2)) ELSE NULL END AS SUGGESTED_15_BELOW,
        CASE WHEN B.SALE_8 > 0 THEN CAST(B.SALE_8 * 7 / 8 * 0.80 AS DECIMAL(18, 2)) ELSE NULL END AS SUGGESTED_20_BELOW
    FROM Base B
)
SELECT
    ITEM_7 AS "7' Item#",
    DESCRIPTION_7 AS "7' Description",
    LOC4_AVAIL_7 AS "7' Gardena Available",
    COST_7 AS "7' Landed Cost",
    SALE_7 AS "7' Current Sale (imp1)",
    CAST(COST_7 / 7 AS DECIMAL(18, 5)) AS "7' Cost per Foot",
    CASE WHEN SALE_7 > 0 THEN CAST(SALE_7 / 7 AS DECIMAL(18, 5)) ELSE NULL END AS "7' Sale per Foot",
    ITEM_8 AS "Matched 8' Item#",
    DESCRIPTION_8 AS "Matched 8' Description",
    MATCH_METHOD AS "Match Method",
    COST_8 AS "8' Landed Cost",
    SALE_8 AS "8' Current Sale (imp1)",
    CAST(COST_8 / 8 AS DECIMAL(18, 5)) AS "8' Cost per Foot",
    CASE WHEN SALE_8 > 0 THEN CAST(SALE_8 / 8 AS DECIMAL(18, 5)) ELSE NULL END AS "8' Sale per Foot",
    SALE_8_NORMALIZED_TO_7 AS "8' Sale Normalized to 7'",
    SUGGESTED_10_BELOW AS "Suggested 7' Sale - 10% Below 8'",
    SUGGESTED_15_BELOW AS "Suggested 7' Sale - 15% Below 8'",
    SUGGESTED_20_BELOW AS "Suggested 7' Sale - 20% Below 8'",
    CASE
        WHEN SUGGESTED_15_BELOW > 0 THEN CAST(DOUBLE(SUGGESTED_15_BELOW - COST_7) / DOUBLE(SUGGESTED_15_BELOW) AS DECIMAL(18, 6))
        ELSE NULL
    END AS "Margin % at 15% Below 8'",
    CASE
        WHEN ITEM_8 IS NULL THEN 'Needs manual 8-foot match'
        WHEN SALE_8 = 0 THEN 'Matched 8-foot item has zero IMP1; review price manually'
        WHEN COST_7 = 0 THEN '7-foot landed cost missing/zero; review cost manually'
        ELSE 'Ready for discount review'
    END AS "Pricing Review Note"
FROM Pricing
ORDER BY ITEM_7
