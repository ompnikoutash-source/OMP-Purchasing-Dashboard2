/* ============================================================
   MARGIN REPORT — Power Query Edition
   DB: DB2/AS400 via ODBC, schema GSFL2K

   INSTRUCTIONS: Each block below is a separate query.
   Paste ONE block at a time into Power Query > New Source >
   Advanced Options > SQL Statement. Do NOT include semicolons.

   RUN ORDER:
     Query A — Validation detail  (confirm field names vs. screen)
     Query B — Validation totals  (confirm On Hand/Avail numbers)
     Query C — PO field discovery (find POLINE column names)
     Query D — Price field discovery (find ITEMMAST price field)
     Query E — MAIN MARGIN REPORT (run after A-D confirmed)
   ============================================================ */


/* ============================================================
   QUERY A — Paste into Power Query as its own query
   Validate ITEMDETL row detail vs. IM07006 screen for ENPSO85801
   Expected: 11 rows, costs per the screen
   ============================================================ */

SELECT
    IDITEM,
    IDLOC,
    IDSERL                    AS SERIAL_NUM,
    IDDATE                    AS RCPT_DATE,
    IDBIN                     AS BIN,
    IDQOH                     AS ON_HAND,
    IDQOH - IDQAL             AS AVAILABLE,    -- IDQAL = allocated/committed
    IDQAL                     AS COMMITTED,
    IDQHLD                    AS HELD,
    IDCOST                    AS COST,
    IDKY                      AS TAG,
    IDBUYR                    AS BUYER,
    IDCOMT                    AS COMMENT
FROM GSFL2K.ITEMDETL
WHERE IDITEM = 'ENPSO85801'
  AND IDLOC  = '001'
ORDER BY IDDATE


/* ============================================================
   QUERY B — Paste into Power Query as its own query
   Validate ITEMDETL totals vs. IM07006 screen footer
   Expected: ROW_COUNT=11  ON_HAND=26  COMMITTED=11  AVAIL=15  HELD=0
   ============================================================ */

SELECT
    IDITEM,
    COUNT(*)              AS ROW_COUNT,
    SUM(IDQOH)            AS TOTAL_ON_HAND,
    SUM(IDQOH - IDQAL)    AS TOTAL_AVAIL,
    SUM(IDQAL)            AS TOTAL_COMMITTED,
    SUM(IDQHLD)           AS TOTAL_HELD
FROM GSFL2K.ITEMDETL
WHERE IDITEM = 'ENPSO85801'
  AND IDLOC  = '001'
GROUP BY IDITEM


/* ============================================================
   QUERY C — Paste into Power Query as its own query
   Discover POLINE column names
   Look for: item, vendor, cost, date, PO number fields
   ============================================================ */

SELECT *
FROM GSFL2K.POLINE
FETCH FIRST 5 ROWS ONLY


/* ============================================================
   QUERY D — Paste into Power Query as its own query
   Discover ITEMMAST price field and VENDMAST name field
   ============================================================ */

SELECT *
FROM GSFL2K.ITEMMAST
FETCH FIRST 3 ROWS ONLY


/* ================================================================
   QUERY E — COPY STARTS ON THE VERY NEXT LINE (the word WITH)
   DO NOT include any lines above this comment when pasting
   ================================================================ */
WITH

CASE1_ITEMS (ITEM, VENDOR, ITEM_GROUP) AS (
    VALUES
    ('GFBHO9501','560',1),('GFBHO9502','560',1),('GFBHO9503','560',1),
    ('GFBHO9505','560',1),('GFBHO9506','560',1),('GFBHO9507','560',1),
    ('GFBHO9508','560',1),('GFBHO9509','560',1),('GFBHO9510','560',1),
    ('GFBHO9511','560',1),
    ('GFLEO7501','560',2),('GFLEO7502','560',2),('GFLEO7503','560',2),
    ('GFLEO7503O','560',2),('GFLEO7504','560',2),('GFLEO7505','560',2),
    ('GFLEO7506','560',2),('GFLEO7507','560',2),
    ('GFLEW7501','560',3),
    ('GFLEO9501','560',3),('GFLEO9502','560',3),('GFLEO9502O','560',3),
    ('GFLEO9503','560',3),('GFLEO9504','560',3),('GFLEO9505','560',3),
    ('GFLEO1001','560',3),('GFLEO1002','560',3),('GFLEO1003','560',3),
    ('GHCCH588','1262',4),('GHCCH589','1262',4),('GHCCH590','1262',4),
    ('GHCCM592','1262',5),('GHCCM593','1262',5),
    ('GHNPO200','1262',6),('GHNPO201','1262',6),('GHNPO206','1262',6),
    ('GHNPO207','1262',6),('GHNPO208','1262',6),('GHNPO210','1262',6),
    ('GHNPO211','1262',6),('GHNPO212','1262',6),
    ('GNPSO85801','2232',7),('GNPSO85802','2232',7),
    ('GNPSO85804','2232',7),('GNPSO85806','2232',7),
    ('GNPSO85803','2232',8),('GNPSO85805','2232',8),
    ('GFLEO7503','2232',9),
    ('GFLEW7501','2232',10),
    ('GFLEO7501','2232',11),('GFLEO7502','2232',11),('GFLEO7504','2232',11),
    ('GFLEO7505','2232',11),('GFLEO7506','2232',11),('GFLEO7507','2232',11),
    ('GFLEO9502','2232',12),('GFLEO9502O','2232',12),
    ('GFLEO9501','2232',13),('GFLEO9504','2232',13),
    ('GFLEO9503','2232',14),('GFLEO9505','2232',14),
    ('GFLEO1001','2232',15),('GFLEO1002','2232',15),('GFLEO1003','2232',15),
    ('GFLEPH501-L','2232',16),
    ('GHEXA5146','2508',17),('GHEXA5147','2508',17),
    ('GHEXA516','2508',17),('GHEXA538','2508',17),
    ('GHEXB538','2508',18),
    ('GHEXPR538','2508',19),
    ('GHEXS538','2508',20),
    ('GHEXSM538','2508',21),
    ('GHEXTI538','2508',22),
    ('GFLEO7501','450',23),('GFLEO7505','450',23),
    ('GFLEO7506','450',23),('GFLEO7507','450',23),
    ('GFLEO9503','450',24),('GFLEO9505','450',24),
    ('GFLEO9504','450',25),('GFLEO1001','450',25),('GFLEO1002','450',25),
    ('GFLEO1003','450',26),
    ('GNPSO85801','450',27),('GNPSO85802','450',27),('GNPSO85803','450',27),
    ('GNPSO85804','450',27),('GNPSO85805','450',27),('GNPSO85806','450',27)
),

CASE2_ITEMS (ITEM, ITEM_GROUP) AS (
    VALUES
    ('UNCOAC12',28),('UNCOAC14',28),
    ('RH2SRS',29),('RH2SRSNB',29),('RQ214SRSSP',29),('RQ314SRSSP',29),
    ('RH2SOSNB',29),('RH2SOSNB-24',29),('RQ214SOSSP',29),('RQ314SOSSP',29),
    ('RH21RS',30),('RQ4SRB',30),('RQ5SRB',30),('RH112QRSNB-24',30),
    ('RH2QRSNB',30),('RH2QRSNB-24',30),('RH112SRSNB-24',30),
    ('RH112SRSNB-15',30),('RQ112SRSNB',30),('RQ214SRS-16',30),
    ('RQ5SRS',30),('RQ7SRS',30),('RT2SRSNB-24',30),('RQ4SRS',30),
    ('RQ3SRS',30),('RQ3SRB',30),('RQ5SRSI',30),('RQ5SWS',30),
    ('RH21OSNB',30),('RQ4SOB',30),('RQ5SOB',30),('RQ6SOB',30),
    ('RQ7SOB',30),('RH112QOSNB-24',30),('RH112QOSNB-15',30),
    ('RH2QOSNB',30),('RH2QOSNB-24',30),('RH2QOSNB-20',30),
    ('RQ214QOSSP',30),('RQ314QOSSP',30),('RH112SOSNB',30),
    ('RH112SOSNB-24',30),('RH112SOSNB-15',30),('RQ112SOSNB',30),
    ('RQ214SOS-16',30),('RQ3SOS',30),('RQ5SOS',30),('RQ6SOS',30),
    ('RQ7SOS',30),('RT112SOS',30),('RT2SOSNB-24',30),('RQ314SDS',30),
    ('RQ5FMS',30),('RQ214FMS',30),('RQ314FMS',30)
),

CASE1_PO_RANKED AS (
    SELECT
        CI.ITEM,
        CI.VENDOR,
        PL.PLCOST                      AS MAT_PRICE,
        ROW_NUMBER() OVER (
            PARTITION BY CI.ITEM, CI.VENDOR
            ORDER BY CASE WHEN PL."PLPO#" IS NULL THEN 0 ELSE PL."PLPO#" END DESC
        ) AS RN
    FROM CASE1_ITEMS CI
    LEFT JOIN GSFL2K.POLINE PL
        ON  TRIM(PL.PLITEM)       = CI.ITEM
        AND TRIM(CHAR(PL.PLVEND)) = CI.VENDOR
),

CASE1_RANKED AS (
    SELECT
        CI.ITEM,
        CI.VENDOR,
        CI.ITEM_GROUP,
        ID.IDCOST                      AS COST,
        ID.IDDATE                      AS RCPT_DATE,
        TRIM(ID.IDSERL)                AS RCPT_TAG,
        ROW_NUMBER() OVER (
            PARTITION BY CI.ITEM, CI.VENDOR
            ORDER BY CASE WHEN ID.IDDATE IS NULL THEN DATE('0001-01-01') ELSE ID.IDDATE END DESC
        ) AS RN
    FROM CASE1_ITEMS CI
    LEFT JOIN GSFL2K.ITEMDETL ID
        ON  TRIM(ID.IDITEM) = CI.ITEM
        AND ID.IDDATE >= DATE('2025-10-01')
        AND ID.IDCOST  > 0
),

CASE1_COSTS AS (
    SELECT
        R.ITEM,
        R.VENDOR,
        R.ITEM_GROUP,
        R.COST,
        R.RCPT_DATE                    AS PO_DATE,
        R.RCPT_TAG                     AS PO_NUMBER,
        'POST_TARIFF_RCPT'             AS COST_METHOD,
        CAST(NULL AS INTEGER)          AS PERIODS_USED,
        DECIMAL(P.MAT_PRICE, 10, 4)    AS MATERIAL_PRICE
    FROM CASE1_RANKED R
    LEFT JOIN CASE1_PO_RANKED P
        ON  P.ITEM   = R.ITEM
        AND P.VENDOR = R.VENDOR
        AND P.RN     = 1
    WHERE R.RN = 1
),

CASE2_RANKED AS (
    SELECT
        CI.ITEM,
        CI.ITEM_GROUP,
        ID.IDCOST                      AS COST,
        ID.IDDATE                      AS RCPT_DATE,
        ROW_NUMBER() OVER (
            PARTITION BY CI.ITEM
            ORDER BY CASE WHEN ID.IDDATE IS NULL THEN DATE('0001-01-01') ELSE ID.IDDATE END DESC
        ) AS RN
    FROM CASE2_ITEMS CI
    LEFT JOIN GSFL2K.ITEMDETL ID
        ON  TRIM(ID.IDITEM) = CI.ITEM
        AND ID.IDDATE >= CURRENT_DATE - 1 YEAR
        AND ID.IDCOST  > 0
),

CASE2_COSTS AS (
    SELECT
        ITEM,
        CAST(NULL AS CHAR(10))         AS VENDOR,
        ITEM_GROUP,
        AVG(COST)                      AS COST,
        CAST(NULL AS DATE)             AS PO_DATE,
        CAST(NULL AS CHAR(10))         AS PO_NUMBER,
        'AVG_5_RCPT'                   AS COST_METHOD,
        COUNT(COST)                    AS PERIODS_USED,
        CAST(NULL AS DECIMAL(10,4))    AS MATERIAL_PRICE
    FROM CASE2_RANKED
    WHERE RN <= 5
    GROUP BY ITEM, ITEM_GROUP
),

ALL_COSTS AS (
    SELECT ITEM, VENDOR, ITEM_GROUP, COST, PO_DATE,
           PO_NUMBER, COST_METHOD, PERIODS_USED, MATERIAL_PRICE
    FROM CASE1_COSTS
    UNION ALL
    SELECT ITEM, VENDOR, ITEM_GROUP, COST, PO_DATE,
           PO_NUMBER, COST_METHOD, PERIODS_USED, MATERIAL_PRICE
    FROM CASE2_COSTS
)

SELECT
    AC.ITEM                            AS SKU,
    TRIM(IM.IMDESC)                    AS DESCRIPTION,
    AC.VENDOR                          AS VENDOR_NUMBER,
    TRIM(VM.VMNAME)                    AS VENDOR_NAME,
    AC.ITEM_GROUP,
    AC.COST_METHOD,
    AC.PERIODS_USED,
    AC.PO_NUMBER,
    AC.PO_DATE                         AS COST_DATE,
    AC.MATERIAL_PRICE,
    DECIMAL(AC.COST,   10, 4)          AS LANDED_COST,
    DECIMAL(IM.IMP1,   10, 4)          AS SALE_PRICE,
    CASE
        WHEN AC.COST > 0
         AND IM.IMP1  > 0
        THEN DECIMAL(
                (IM.IMP1 - AC.COST) / IM.IMP1,
             8, 4)
        ELSE NULL
    END                                AS MARGIN_PCT
FROM ALL_COSTS AC
LEFT JOIN GSFL2K.ITEMMAST IM
    ON TRIM(IM.IMITEM) = AC.ITEM
LEFT JOIN GSFL2K.VENDMAST VM
    ON TRIM(CHAR(VM.VMVEND)) = AC.VENDOR
ORDER BY
    AC.ITEM_GROUP,
    AC.ITEM
