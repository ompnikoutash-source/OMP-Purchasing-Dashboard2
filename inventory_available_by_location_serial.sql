/* ============================================================
   INVENTORY AVAILABLE BY LOCATION WITH SERIAL NUMBERS
   DB: DB2/AS400 via ODBC, schema GSFL2K

   Collections: Garrison II Distressed, Garrison II Smooth,
                Newport, Illumin8, Exotics, Carolina Classic

   One row per physical inventory tag (serial).
   Available SF = (IDQOH - IDQAL) converted to SF via IMFACT,
   applied conditionally (no conversion when IMFACT=0 or UOMs match).

   Location columns show SF for that serial's location only;
   all other location columns show 0.

   Vendor is sourced from ITEMRECH (IRVEND) for the specific serial
   using the most recent receipt (highest IRRECNBR).
   ============================================================ */

WITH

SerialVendor AS (
    SELECT
        TRIM(R.IRITEM)          AS ITEM_NUMBER,
        TRIM(R.IRSERL)          AS SERIAL,
        TRIM(CHAR(R.IRVEND))    AS VENDOR_NUMBER,
        R.IRDATE                AS RECEIVED_DATE
    FROM GSFL2K.ITEMRECH R
    WHERE R.IRCO = 1
      AND TRIM(R.IRSRC) = 'P'
      AND R.IRRECNBR = (
          SELECT MAX(R2.IRRECNBR)
          FROM GSFL2K.ITEMRECH R2
          WHERE TRIM(R2.IRITEM) = TRIM(R.IRITEM)
            AND TRIM(R2.IRSERL) = TRIM(R.IRSERL)
            AND R2.IRCO = 1
            AND TRIM(R2.IRSRC) = 'P'
      )
)

SELECT
    TRIM(IM.IMITEM)    AS "Item Number",
    TRIM(IM.IMDESC)    AS "Description",
    TRIM(IX.IMCOLLECT) AS "Collection",

    DECIMAL(CASE WHEN ID.IDLOC = 1 THEN
        CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
             THEN (ID.IDQOH - ID.IDQAL)
             ELSE (ID.IDQOH - ID.IDQAL) * IM.IMFACT END
    ELSE 0 END, 18, 2) AS "Available SF Loc 1",

    DECIMAL(CASE WHEN ID.IDLOC = 3 THEN
        CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
             THEN (ID.IDQOH - ID.IDQAL)
             ELSE (ID.IDQOH - ID.IDQAL) * IM.IMFACT END
    ELSE 0 END, 18, 2) AS "Available SF Loc 3",

    DECIMAL(CASE WHEN ID.IDLOC = 4 THEN
        CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
             THEN (ID.IDQOH - ID.IDQAL)
             ELSE (ID.IDQOH - ID.IDQAL) * IM.IMFACT END
    ELSE 0 END, 18, 2) AS "Available SF Loc 4",

    DECIMAL(CASE WHEN ID.IDLOC = 5 THEN
        CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
             THEN (ID.IDQOH - ID.IDQAL)
             ELSE (ID.IDQOH - ID.IDQAL) * IM.IMFACT END
    ELSE 0 END, 18, 2) AS "Available SF Loc 5",

    DECIMAL(CASE WHEN ID.IDLOC = 6 THEN
        CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
             THEN (ID.IDQOH - ID.IDQAL)
             ELSE (ID.IDQOH - ID.IDQAL) * IM.IMFACT END
    ELSE 0 END, 18, 2) AS "Available SF Loc 6",

    DECIMAL(CASE WHEN ID.IDLOC = 8 THEN
        CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
             THEN (ID.IDQOH - ID.IDQAL)
             ELSE (ID.IDQOH - ID.IDQAL) * IM.IMFACT END
    ELSE 0 END, 18, 2) AS "Available SF Loc 8",

    DECIMAL(CASE WHEN ID.IDLOC = 9 THEN
        CASE WHEN IM.IMFACT = 0 OR IM.IMUM1 = IM.IMUM2
             THEN (ID.IDQOH - ID.IDQAL)
             ELSE (ID.IDQOH - ID.IDQAL) * IM.IMFACT END
    ELSE 0 END, 18, 2) AS "Available SF Loc 9",

    TRIM(ID.IDSERL)        AS "Serial #",
    SV.RECEIVED_DATE       AS "Date Received",
    TRIM(SV.VENDOR_NUMBER) AS "Vendor Number",
    TRIM(VM.VMNAME)        AS "Vendor Name"

FROM GSFL2K.ITEMMAST IM
JOIN GSFL2K.ITEMXTRA IX
  ON IX.IMXITM = IM.IMITEM
JOIN GSFL2K.ITEMDETL ID
  ON TRIM(ID.IDITEM) = TRIM(IM.IMITEM)
 AND ID.IDLOC IN (1, 3, 4, 5, 6, 8, 9)
LEFT JOIN SerialVendor SV
  ON SV.ITEM_NUMBER = TRIM(IM.IMITEM)
 AND SV.SERIAL      = TRIM(ID.IDSERL)
LEFT JOIN GSFL2K.VENDMAST VM
  ON VM.VMVEND = SV.VENDOR_NUMBER
WHERE IM.IMDIV = 1
  AND UPPER(TRIM(IX.IMCOLLECT)) IN (
      'GARRISON II DISTRESSED',
      'GARRISON II SMOOTH',
      'NEWPORT',
      'ILLUMIN8',
      'EXOTICS',
      'CAROLINA CLASSIC'
  )
  AND (ID.IDQOH - ID.IDQAL) > 0
ORDER BY
    TRIM(IM.IMITEM),
    ID.IDLOC,
    TRIM(ID.IDSERL)
