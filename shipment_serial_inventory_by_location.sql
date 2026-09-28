-- =============================================================================
-- Shipment Serial/Lot Inventory by Location
-- Shows all lot/serial numbers received on a specific arrival date,
-- with current available SF per location as individual columns.
--
-- HOW TO USE:
--   Replace the date in the WHERE clause (line ~55) with your target arrival date.
--   Example:  AND IR.IRDATE = DATE('2025-12-15')
--
-- Available SF = (IDQOH - IDQAL) * IMFACT  (converts bundles → square feet)
-- Locations shown: 1, 3, 4, 5, 6, 8, 9, 51
-- =============================================================================

WITH received_serials AS (
    -- One row per distinct item + serial received on the target date.
    -- Using MAX(IRRECNBR) handles cases where the same serial was received
    -- in multiple receipt transactions on the same day.
    SELECT
        TRIM(IR.IRITEM)          AS item_number,
        IR.IRSERL                AS serial_lot,
        MIN(IR.IRDATE)           AS received_date,
        MAX(IR.IRVEND)           AS vendor_number
    FROM GSFL2K.ITEMRECH IR
    WHERE IR.IRCO  = 1
      AND IR.IRSRC = 'P'
      -- *** CHANGE THIS DATE TO YOUR TARGET ARRIVAL DATE ***
      AND IR.IRDATE = DATE('2025-12-15')
    GROUP BY
        TRIM(IR.IRITEM),
        IR.IRSERL
),

item_info AS (
    SELECT
        TRIM(IM.IMITEM)          AS item_number,
        TRIM(IM.IMDESC)          AS description,
        IM.IMFACT                AS conv_factor
    FROM GSFL2K.ITEMMAST IM
),

-- Current available inventory per tag, converted to SF, by location.
-- Available = IDQOH - IDQAL  (on-hand minus allocated), then * IMFACT for SF.
inventory_pivot AS (
    SELECT
        TRIM(ID.IDITEM)          AS item_number,
        ID.IDSERL                AS serial_lot,
        SUM(CASE WHEN ID.IDLOC =  1 THEN (ID.IDQOH - ID.IDQAL) * II.conv_factor ELSE 0 END) AS loc_1_sf,
        SUM(CASE WHEN ID.IDLOC =  3 THEN (ID.IDQOH - ID.IDQAL) * II.conv_factor ELSE 0 END) AS loc_3_sf,
        SUM(CASE WHEN ID.IDLOC =  4 THEN (ID.IDQOH - ID.IDQAL) * II.conv_factor ELSE 0 END) AS loc_4_sf,
        SUM(CASE WHEN ID.IDLOC =  5 THEN (ID.IDQOH - ID.IDQAL) * II.conv_factor ELSE 0 END) AS loc_5_sf,
        SUM(CASE WHEN ID.IDLOC =  6 THEN (ID.IDQOH - ID.IDQAL) * II.conv_factor ELSE 0 END) AS loc_6_sf,
        SUM(CASE WHEN ID.IDLOC =  8 THEN (ID.IDQOH - ID.IDQAL) * II.conv_factor ELSE 0 END) AS loc_8_sf,
        SUM(CASE WHEN ID.IDLOC =  9 THEN (ID.IDQOH - ID.IDQAL) * II.conv_factor ELSE 0 END) AS loc_9_sf,
        SUM(CASE WHEN ID.IDLOC = 51 THEN (ID.IDQOH - ID.IDQAL) * II.conv_factor ELSE 0 END) AS loc_51_sf
    FROM GSFL2K.ITEMDETL ID
    JOIN item_info II
        ON TRIM(ID.IDITEM) = II.item_number
    WHERE ID.IDCO = 1
      AND (ID.IDDELT IS NULL OR ID.IDDELT <> 'D')
      AND ID.IDLOC IN (1, 3, 4, 5, 6, 8, 9, 51)
    GROUP BY
        TRIM(ID.IDITEM),
        ID.IDSERL
)

SELECT
    RS.item_number                             AS "Item",
    II.description                             AS "Description",
    RS.vendor_number                           AS "Vendor #",
    TRIM(VM.VMNAME)                            AS "Vendor Name",
    RS.serial_lot                              AS "Serial / Lot",
    RS.received_date                           AS "Date Received",
    COALESCE(IP.loc_1_sf,  0)                  AS "Loc 1 (SF)",
    COALESCE(IP.loc_3_sf,  0)                  AS "Loc 3 (SF)",
    COALESCE(IP.loc_4_sf,  0)                  AS "Loc 4 (SF)",
    COALESCE(IP.loc_5_sf,  0)                  AS "Loc 5 (SF)",
    COALESCE(IP.loc_6_sf,  0)                  AS "Loc 6 (SF)",
    COALESCE(IP.loc_8_sf,  0)                  AS "Loc 8 (SF)",
    COALESCE(IP.loc_9_sf,  0)                  AS "Loc 9 (SF)",
    COALESCE(IP.loc_51_sf, 0)                  AS "Loc 51 (SF)",
    -- Total available SF across all locations
    (  COALESCE(IP.loc_1_sf,  0)
     + COALESCE(IP.loc_3_sf,  0)
     + COALESCE(IP.loc_4_sf,  0)
     + COALESCE(IP.loc_5_sf,  0)
     + COALESCE(IP.loc_6_sf,  0)
     + COALESCE(IP.loc_8_sf,  0)
     + COALESCE(IP.loc_9_sf,  0)
     + COALESCE(IP.loc_51_sf, 0)
    )                                          AS "Total Avail (SF)"

FROM received_serials RS

JOIN item_info II
    ON RS.item_number = II.item_number

LEFT JOIN GSFL2K.VENDMAST VM
    ON RS.vendor_number = VM.VMVEND

LEFT JOIN inventory_pivot IP
    ON RS.item_number = IP.item_number
   AND RS.serial_lot  = IP.serial_lot

ORDER BY
    RS.item_number,
    RS.serial_lot
