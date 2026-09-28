/*
   Gartman General Ledger detail pull for account 7400 / ADVERTISING.

   Screen reference: GL31002CTL General Ledger Detail File Inquiry.
   Confirmed field mapping:
     Date        -> GLDETL.GDDATE
     Source      -> GLDETL.GDSRC
     Description -> GLDETL.GDDESC
     Reference   -> GLDETL.GDJRFC + GLDETL.GDJRF#
     Amount      -> GLDETL.GDAMT
*/

SELECT
    D.GDDATE AS "DATE",
    TRIM(D.GDSRC) AS "SOURCE",
    TRIM(D.GDDESC) AS "DESCRIPTION",
    TRIM(D.GDJRFC) ||
        CASE
            WHEN TRIM(D.GDJRF#) <> ''
            THEN ' ' || TRIM(D.GDJRF#)
            ELSE ''
        END AS "REFERENCE",
    D.GDAMT AS "AMOUNT"
FROM GSFL2K.GLDETL D
WHERE D.GDCO = 1
  AND D.GDGL# = 7400
  AND D.GDDATE BETWEEN DATE('2024-01-01') AND DATE('2026-09-08')
ORDER BY
    D.GDDATE,
    D.GDSRC,
    D.GDDESC,
    D.GDNO
