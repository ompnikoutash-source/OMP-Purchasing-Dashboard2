/* ============================================================
   DIVISION 7 VENDORS — All vendors from whom OMP has ordered
   Division 7 items (based on purchase receipt history).

   DB: DB2/AS400 via ODBC, schema GSFL2K

   Columns:
     VENDOR_NUMBER    -- VM.VMVEND
     VENDOR_NAME      -- VM.VMNAME
     DISTINCT_ITEMS   -- count of unique Div 7 items ordered
     FIRST_ORDER      -- earliest receipt date
     LAST_ORDER       -- most recent receipt date
   ============================================================ */

SELECT
    IR.IRVEND                        AS VENDOR_NUMBER,
    TRIM(VM.VMNAME)                  AS VENDOR_NAME,
    COUNT(DISTINCT TRIM(IR.IRITEM))  AS DISTINCT_ITEMS,
    MIN(IR.IRDATE)                   AS FIRST_ORDER,
    MAX(IR.IRDATE)                   AS LAST_ORDER
FROM GSFL2K.ITEMRECH IR
JOIN GSFL2K.ITEMMAST IM
    ON  TRIM(IR.IRITEM) = TRIM(IM.IMITEM)
    AND IM.IMDIV = 7
LEFT JOIN GSFL2K.VENDMAST VM
    ON  IR.IRVEND = VM.VMVEND
WHERE IR.IRSRC = 'P'
GROUP BY
    IR.IRVEND,
    TRIM(VM.VMNAME)
ORDER BY
    TRIM(VM.VMNAME)
