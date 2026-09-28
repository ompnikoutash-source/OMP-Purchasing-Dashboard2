/*
   Run after final_order_notes_01_load_temp_table.sql, in the same SQL session/connection.
   Returns one row per raw note line. This is the fallback if LISTAGG has trouble.
*/

WITH NOTE_LINES AS (
    SELECT
        L.LIST_ROW,
        1 AS SOURCE_SORT,
        CAST('SHTEXT' AS VARCHAR(10)) AS NOTE_SOURCE,
        T.STSEQ# AS NOTE_SEQUENCE,
        RRN(T) AS NOTE_RRN,
        TRIM(COALESCE(T.STCMT1, '')) AS NOTE_LINE_1,
        TRIM(COALESCE(T.STCMT2, '')) AS NOTE_LINE_2,
        CAST(
            TRIM(COALESCE(T.STCMT1, '')) ||
            CASE
                WHEN TRIM(COALESCE(T.STCMT2, '')) <> ''
                THEN ' ' || TRIM(COALESCE(T.STCMT2, ''))
                ELSE ''
            END
            AS VARCHAR(500)
        ) AS NOTE_TEXT
    FROM SESSION.FINAL_ORDER_LIST L
    JOIN GSFL2K.SHTEXT T
      ON T.STORD# = L.ORDER_NUMBER
    WHERE TRIM(COALESCE(T.STCMT1, '')) <> ''
       OR TRIM(COALESCE(T.STCMT2, '')) <> ''

    UNION ALL

    SELECT
        L.LIST_ROW,
        2 AS SOURCE_SORT,
        CAST('OOTEXT' AS VARCHAR(10)) AS NOTE_SOURCE,
        T.OTSEQ# AS NOTE_SEQUENCE,
        RRN(T) AS NOTE_RRN,
        TRIM(COALESCE(T.OTCMT1, '')) AS NOTE_LINE_1,
        TRIM(COALESCE(T.OTCMT2, '')) AS NOTE_LINE_2,
        CAST(
            TRIM(COALESCE(T.OTCMT1, '')) ||
            CASE
                WHEN TRIM(COALESCE(T.OTCMT2, '')) <> ''
                THEN ' ' || TRIM(COALESCE(T.OTCMT2, ''))
                ELSE ''
            END
            AS VARCHAR(500)
        ) AS NOTE_TEXT
    FROM SESSION.FINAL_ORDER_LIST L
    JOIN GSFL2K.OOTEXT T
      ON T.OTORD# = L.ORDER_NUMBER
    WHERE TRIM(COALESCE(T.OTCMT1, '')) <> ''
       OR TRIM(COALESCE(T.OTCMT2, '')) <> ''
)
SELECT
    L.LIST_ROW AS WORKBOOK_ROW,
    L.ORDER_DATE,
    TRIM(CHAR(L.ORDER_NUMBER)) AS ORDER_NUMBER,
    COALESCE(L.INVOICE_NUMBER, '') AS INVOICE_NUMBER,
    COALESCE(L.ACCOUNT_NUMBER, '') AS ACCOUNT_NUMBER,
    COALESCE(N.NOTE_SOURCE, '') AS NOTE_SOURCE,
    N.NOTE_SEQUENCE,
    N.NOTE_RRN,
    COALESCE(N.NOTE_LINE_1, '') AS NOTE_LINE_1,
    COALESCE(N.NOTE_LINE_2, '') AS NOTE_LINE_2,
    COALESCE(N.NOTE_TEXT, '') AS NOTE_TEXT
FROM SESSION.FINAL_ORDER_LIST L
LEFT JOIN NOTE_LINES N
  ON N.LIST_ROW = L.LIST_ROW
ORDER BY
    L.LIST_ROW,
    N.SOURCE_SORT,
    N.NOTE_SEQUENCE,
    N.NOTE_RRN;
