/* ============================================================
   3M ITEMS — MINIMUM ORDER QUANTITY
   DB: DB2/AS400 via ODBC, schema GSFL2K

   NOTE: Verify the MOQ field name before running.
   Likely candidates on ITEMMAST:
     IMMOQ  — minimum order quantity  (most common)
     IMMINQ — minimum quantity
     IMEOQ  — economic order quantity
   Run the comment-block at the bottom to inspect available
   columns if the query fails.
   ============================================================ */

SELECT
    TRIM(IM.IMITEM)  AS ITEM_NUMBER,
    IM.IMMOQ         AS MIN_ORDER_QTY
FROM GSFL2K.ITEMMAST IM
WHERE TRIM(IM.IMITEM) IN (
    'BD3M16122',
    'BD3M16162',
    'BE3MG12100',
    'BE3MG12120',
    'BE3MG1240',
    'BE3MG1260',
    'BE3MH12100',
    'BE3MH1240',
    'BE3MH1280',
    'BE3M1040',
    'BE3M1080',
    'BE3M8100',
    'BE3M8120',
    'BE3M836',
    'BE3M840',
    'BE3M850',
    'BE3M860',
    'BE3M880',
    'BP3MMAR16',
    'BP3MMAR16S',
    'BP3MRED16',
    'BP3MWHDI16',
    'DM3M8210',
    'DM3M8511',
    'DP3M12100',
    'DP3M1240',
    'DP3M1250',
    'DP3M1260',
    'DP3M1280',
    'ED3M1005',
    'ED3M1007',
    'ED3M1205',
    'ED3M1207',
    'ED3M127',
    'ED3M247',
    'ED3M365',
    'ED3M367',
    'ED3M405',
    'ED3M407',
    'ED3M505',
    'ED3M507',
    'ED3M605',
    'ED3M607',
    'ED3M805',
    'ED3M807',
    'EP3M03647',
    'EP3M03648',
    'EP3M09152',
    'EP3M09153',
    'ES3M1005',
    'ES3M1007',
    'SS3M16100',
    'SS3M16120',
    'SS3M16150',
    'SS3M16180',
    'SS3M16220',
    'SS3M1660',
    'SS3M1680',
    'TA3M02993',
    'TA3M04365',
    'TA3M06819',
    'TA3M06820',
    'TA3M79749',
    'DP3M12100C',
    'DP3M1236C',
    'DP3M1240C',
    'DP3M1250C',
    'DP3M1260C',
    'DP3M850C',
    'DP3M1280C',
    'BE3MG1280',
    'DM3M6807',
    'AB3M86434',
    'HI3M997578',
    'HI3M997547',
    'HI3M997646',
    'ES3M1003',
    'ED3M7RQC80',
    'HI3M5RQC1005',
    'HI3M5RQC808',
    'HI3M5RQC1008',
    'HI3M5RQC1208',
    'HI3M5RQC805',
    'HI3M8RQC120',
    'EP3M905434DC',
    'TA3M8087CW'
)
ORDER BY TRIM(IM.IMITEM)
FOR READ ONLY

/* ── Field discovery (run this if IMMOQ doesn't exist) ──────────────────
   SELECT COLUMN_NAME, DATA_TYPE, LENGTH
   FROM QSYS2.SYSCOLUMNS
   WHERE TABLE_SCHEMA = 'GSFL2K'
     AND TABLE_NAME   = 'ITEMMAST'
     AND UPPER(COLUMN_NAME) LIKE '%MOQ%'
        OR UPPER(COLUMN_NAME) LIKE '%MIN%'
        OR UPPER(COLUMN_NAME) LIKE '%EOQ%'
   ORDER BY COLUMN_NAME
   ──────────────────────────────────────────────────────────────────── */
