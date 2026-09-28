/* ============================================================
   INVENTORY REPORT — Division 1, Location 9
   DB: DB2/AS400 via ODBC, schema GSFL2K

   One row per inventory receipt tag for all Division 1 items
   physically at location 9.

   Columns:
     ITEM_NUMBER    -- IM.IMITEM
     DESCRIPTION    -- IM.IMDESC
     COLLECTION     -- IM.IMCOLR
     VENDOR_NUMBER  -- IM.IMVEND
     VENDOR_NAME    -- VM.VMNAME
     TAG_NUM        -- ID.IDKY    (inventory receipt tag)
     SERIAL_NUM     -- ID.IDSERL
     LOT_NUM        -- ID.IDDYLT  (dye lot)
     ON_HAND        -- ID.IDQOH
     AVAILABLE      -- ID.IDQOH - ID.IDQAL
   ============================================================ */

SELECT
    TRIM(IM.IMITEM)                     AS ITEM_NUMBER,
    TRIM(IM.IMDESC)                     AS DESCRIPTION,
    TRIM(IM.IMCOLR)                     AS COLLECTION,
    TRIM(CHAR(IM.IMVEND))               AS VENDOR_NUMBER,
    TRIM(VM.VMNAME)                     AS VENDOR_NAME,
    TRIM(ID.IDKY)                       AS TAG_NUM,
    TRIM(ID.IDSERL)                     AS SERIAL_NUM,
    TRIM(ID.IDDYLT)                     AS LOT_NUM,
    ID.IDQOH                            AS ON_HAND,
    ID.IDQOH - ID.IDQAL                 AS AVAILABLE
FROM GSFL2K.ITEMMAST IM
JOIN GSFL2K.ITEMDETL ID
    ON  TRIM(ID.IDITEM) = TRIM(IM.IMITEM)
    AND ID.IDLOC        = 9
LEFT JOIN GSFL2K.VENDMAST VM
    ON  TRIM(CHAR(VM.VMVEND)) = TRIM(CHAR(IM.IMVEND))
WHERE IM.IMDIV = 1
ORDER BY
    TRIM(IM.IMCOLR),
    TRIM(IM.IMITEM),
    ID.IDDATE           DESC
