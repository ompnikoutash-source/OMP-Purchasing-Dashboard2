-- Diagnostic: GFBHO9501 inventory balance by location
-- Compare to Gartman totals: 1156 on hand, 801 committed, 355 balance

WITH BASE AS (
    SELECT
        B.IBLOC                                    AS LOCATION,
        CASE WHEN B.IBLOC IN (90, 17, 41, 46)
             THEN 'EXCLUDED' ELSE 'INCLUDED' END   AS DASHBOARD_STATUS,
        B.IBQOH                                    AS ON_HAND_CTN,
        B.IBQOO                                    AS COMMITTED_CTN,
        (B.IBQOH - B.IBQOO)                        AS BALANCE_CTN,
        M.IMFACT                                   AS SF_PER_CTN,
        CASE WHEN COALESCE(M.IMFACT, 0) = 0
                  OR TRIM(M.IMUM1) = TRIM(M.IMUM2)
             THEN (B.IBQOH - B.IBQOO)
             ELSE (B.IBQOH - B.IBQOO) * M.IMFACT
        END                                        AS BALANCE_SF,
        TRIM(M.IMUM1)                              AS UM1,
        TRIM(M.IMUM2)                              AS UM2
    FROM GSFL2K.ITEMBAL B
    JOIN GSFL2K.ITEMMAST M ON M.IMITEM = B.IBITEM
    WHERE TRIM(B.IBITEM) = 'GFBHO9501'
)
SELECT LOCATION, DASHBOARD_STATUS, ON_HAND_CTN, COMMITTED_CTN,
       BALANCE_CTN, SF_PER_CTN, BALANCE_SF, UM1, UM2
FROM BASE

UNION ALL

SELECT
    -1, '== INCLUDED TOTAL ==',
    SUM(ON_HAND_CTN), SUM(COMMITTED_CTN), SUM(BALANCE_CTN),
    MAX(SF_PER_CTN), SUM(BALANCE_SF),
    MIN(UM1), MIN(UM2)
FROM BASE
WHERE DASHBOARD_STATUS = 'INCLUDED'

UNION ALL

SELECT
    -2, '== EXCLUDED TOTAL ==',
    SUM(ON_HAND_CTN), SUM(COMMITTED_CTN), SUM(BALANCE_CTN),
    MAX(SF_PER_CTN), SUM(BALANCE_SF),
    MIN(UM1), MIN(UM2)
FROM BASE
WHERE DASHBOARD_STATUS = 'EXCLUDED'

ORDER BY LOCATION
