/* ============================================================
   COLLECTION SALES CONTACTS - MARKETING LIST V2
   DB: DB2/AS400 via ODBC, schema GSFL2K

   PowerQuery use:
     Edit only the two dates in DateRange unless you need to change
     the county ZIP list.

   Changes from the original:
     - Uses a date range instead of current-year/current-month logic.
     - Replaces COLOR with item DESCRIPTION.
     - Excludes customer class 200.
     - Keeps only orders where the ship-to ZIP or customer account ZIP
       falls in LA, Ventura, Orange, Riverside, or Kern county ZIPs.
     - Excludes whole orders where the only Division 1 items are from
       REGULAR UNF or UNF. THE "6".
     - Keeps orders even when no primary contact exists.
   ============================================================ */

WITH

DateRange (START_DATE, END_DATE) AS (
    VALUES (DATE('2025-01-01'), DATE('2026-01-01'))
),

EligibleZipRanges (ZIP_START, ZIP_END) AS (
    VALUES
        (90001, 90008),
        (90010, 90029),
        (90031, 90049),
        (90056, 90059),
        (90061, 90069),
        (90071, 90071),
        (90077, 90077),
        (90201, 90201),
        (90210, 90212),
        (90220, 90222),
        (90230, 90230),
        (90232, 90232),
        (90240, 90242),
        (90245, 90245),
        (90247, 90250),
        (90254, 90255),
        (90260, 90263),
        (90265, 90266),
        (90270, 90270),
        (90272, 90272),
        (90274, 90275),
        (90277, 90278),
        (90280, 90280),
        (90290, 90293),
        (90301, 90305),
        (90401, 90405),
        (90501, 90505),
        (90601, 90606),
        (90620, 90621),
        (90623, 90623),
        (90630, 90631),
        (90638, 90638),
        (90640, 90640),
        (90650, 90650),
        (90660, 90660),
        (90670, 90670),
        (90680, 90680),
        (90701, 90701),
        (90703, 90704),
        (90706, 90706),
        (90710, 90710),
        (90712, 90713),
        (90715, 90717),
        (90720, 90720),
        (90723, 90723),
        (90731, 90732),
        (90740, 90740),
        (90742, 90746),
        (90802, 90808),
        (90810, 90810),
        (90813, 90815),
        (90822, 90822),
        (91001, 91001),
        (91006, 91007),
        (91010, 91011),
        (91016, 91016),
        (91020, 91020),
        (91024, 91024),
        (91030, 91030),
        (91040, 91040),
        (91042, 91042),
        (91101, 91101),
        (91103, 91108),
        (91201, 91208),
        (91214, 91214),
        (91301, 91304),
        (91306, 91307),
        (91311, 91311),
        (91316, 91316),
        (91320, 91321),
        (91324, 91326),
        (91331, 91331),
        (91335, 91335),
        (91340, 91340),
        (91342, 91345),
        (91350, 91352),
        (91354, 91356),
        (91360, 91362),
        (91364, 91364),
        (91367, 91367),
        (91377, 91377),
        (91381, 91381),
        (91384, 91384),
        (91401, 91403),
        (91405, 91406),
        (91411, 91411),
        (91423, 91423),
        (91436, 91436),
        (91501, 91502),
        (91504, 91506),
        (91601, 91602),
        (91604, 91608),
        (91702, 91702),
        (91706, 91706),
        (91711, 91711),
        (91719, 91719),
        (91722, 91724),
        (91731, 91733),
        (91740, 91741),
        (91744, 91746),
        (91748, 91748),
        (91750, 91750),
        (91752, 91752),
        (91754, 91755),
        (91759, 91759),
        (91765, 91768),
        (91770, 91770),
        (91773, 91773),
        (91775, 91776),
        (91780, 91780),
        (91789, 91792),
        (91801, 91801),
        (91803, 91803),
        (92201, 92201),
        (92203, 92203),
        (92210, 92211),
        (92220, 92220),
        (92223, 92223),
        (92225, 92225),
        (92230, 92230),
        (92234, 92234),
        (92236, 92236),
        (92239, 92241),
        (92253, 92254),
        (92258, 92258),
        (92260, 92260),
        (92262, 92262),
        (92264, 92264),
        (92270, 92270),
        (92274, 92274),
        (92276, 92276),
        (92282, 92282),
        (92320, 92320),
        (92501, 92501),
        (92503, 92509),
        (92518, 92518),
        (92530, 92530),
        (92532, 92532),
        (92536, 92536),
        (92539, 92539),
        (92543, 92545),
        (92548, 92549),
        (92551, 92551),
        (92553, 92553),
        (92555, 92555),
        (92557, 92557),
        (92561, 92563),
        (92567, 92567),
        (92570, 92571),
        (92582, 92587),
        (92590, 92592),
        (92595, 92596),
        (92602, 92602),
        (92604, 92604),
        (92606, 92606),
        (92610, 92610),
        (92612, 92612),
        (92614, 92614),
        (92618, 92618),
        (92620, 92620),
        (92624, 92627),
        (92629, 92630),
        (92646, 92649),
        (92651, 92651),
        (92653, 92653),
        (92655, 92657),
        (92660, 92663),
        (92672, 92673),
        (92675, 92677),
        (92679, 92679),
        (92683, 92683),
        (92688, 92688),
        (92691, 92692),
        (92694, 92694),
        (92701, 92701),
        (92703, 92708),
        (92780, 92780),
        (92782, 92782),
        (92801, 92802),
        (92804, 92808),
        (92821, 92821),
        (92823, 92823),
        (92831, 92833),
        (92835, 92835),
        (92840, 92841),
        (92843, 92845),
        (92860, 92861),
        (92865, 92870),
        (92879, 92883),
        (92886, 92887),
        (93001, 93001),
        (93003, 93004),
        (93010, 93010),
        (93012, 93012),
        (93015, 93015),
        (93021, 93023),
        (93030, 93030),
        (93033, 93033),
        (93035, 93035),
        (93040, 93041),
        (93060, 93060),
        (93063, 93063),
        (93065, 93066),
        (93203, 93203),
        (93205, 93206),
        (93215, 93216),
        (93222, 93222),
        (93224, 93226),
        (93238, 93238),
        (93240, 93241),
        (93243, 93243),
        (93249, 93252),
        (93255, 93255),
        (93263, 93263),
        (93268, 93268),
        (93276, 93276),
        (93280, 93280),
        (93283, 93283),
        (93285, 93285),
        (93287, 93287),
        (93301, 93301),
        (93304, 93309),
        (93311, 93313),
        (93501, 93501),
        (93505, 93505),
        (93510, 93510),
        (93516, 93516),
        (93518, 93518),
        (93523, 93523),
        (93527, 93528),
        (93531, 93532),
        (93534, 93536),
        (93543, 93544),
        (93550, 93555),
        (93560, 93561),
        (93563, 93563),
        (93591, 93591)
),

OrderAddresses AS (
    SELECT DISTINCT
        SL.SLCO AS COMPANY,
        SL.SLLOC AS LOCATION,
        SL."SLORD#" AS ORDER_NUMBER,
        SL."SLREL#" AS RELEASE_NUMBER,
        SL.SLINV# AS INVOICE_NUMBER,
        TRIM(SH.SHCUST) AS ACCT,
        DECIMAL(COALESCE(CM.CMCLAS, 0), 3, 0) AS CUSTOMER_CLASS,
        CASE
            WHEN COALESCE(SH.SHZIP, 0) >= 100000000 THEN INTEGER(SH.SHZIP / 10000)
            WHEN COALESCE(SH.SHZIP, 0) BETWEEN 10000 AND 99999 THEN INTEGER(SH.SHZIP)
            ELSE NULL
        END AS SHIP_ZIP5,
        CASE
            WHEN COALESCE(CM.CMZIP, 0) >= 100000000 THEN INTEGER(CM.CMZIP / 10000)
            WHEN COALESCE(CM.CMZIP, 0) BETWEEN 10000 AND 99999 THEN INTEGER(CM.CMZIP)
            ELSE NULL
        END AS ACCOUNT_ZIP5
    FROM GSFL2K.SHLINE SL
    JOIN GSFL2K.SHHEAD SH
      ON SL.SLCO = SH.SHCO
     AND SL.SLLOC = SH.SHLOC
     AND SL."SLORD#" = SH.SHORD#
     AND SL.SLINV# = SH.SHINV#
    CROSS JOIN DateRange DR
    LEFT JOIN GSFL2K.CUSTMAST CM
      ON CM.CMCO = SL.SLCO
     AND TRIM(CM.CMCUST) = TRIM(SL.SLCUST)
    WHERE SL.SLDATE BETWEEN DR.START_DATE AND DR.END_DATE
      AND COALESCE(CM.CMCLAS, 0) <> 200
),

EligibleOrders AS (
    SELECT
        OA.*,
        CASE
            WHEN MAX(CASE WHEN SZ.ZIP_START IS NOT NULL THEN 1 ELSE 0 END) = 1
                THEN 'Ship-to ZIP'
            ELSE 'Customer account ZIP'
        END AS ZIP_MATCH_SOURCE
    FROM OrderAddresses OA
    LEFT JOIN EligibleZipRanges SZ
      ON OA.SHIP_ZIP5 BETWEEN SZ.ZIP_START AND SZ.ZIP_END
    LEFT JOIN EligibleZipRanges AZ
      ON OA.ACCOUNT_ZIP5 BETWEEN AZ.ZIP_START AND AZ.ZIP_END
    WHERE SZ.ZIP_START IS NOT NULL
       OR AZ.ZIP_START IS NOT NULL
    GROUP BY
        OA.COMPANY,
        OA.LOCATION,
        OA.ORDER_NUMBER,
        OA.RELEASE_NUMBER,
        OA.INVOICE_NUMBER,
        OA.ACCT,
        OA.CUSTOMER_CLASS,
        OA.SHIP_ZIP5,
        OA.ACCOUNT_ZIP5
),

OrderDivision1Mix AS (
    SELECT
        L.SLCO AS COMPANY,
        L.SLLOC AS LOCATION,
        L."SLORD#" AS ORDER_NUMBER,
        L."SLREL#" AS RELEASE_NUMBER,
        L.SLINV# AS INVOICE_NUMBER,
        SUM(
            CASE
                WHEN IM.IMDIV = 1 THEN 1
                ELSE 0
            END
        ) AS DIVISION_1_LINE_COUNT,
        SUM(
            CASE
                WHEN IM.IMDIV = 1
                 AND UPPER(TRIM(COALESCE(IMX.IMCOLLECT, ''))) NOT IN ('REGULAR UNF', 'UNF. THE "6"')
                    THEN 1
                ELSE 0
            END
        ) AS NON_UNF_DIVISION_1_LINE_COUNT
    FROM GSFL2K.SHLINE L
    JOIN EligibleOrders EO
      ON EO.COMPANY = L.SLCO
     AND EO.LOCATION = L.SLLOC
     AND EO.ORDER_NUMBER = L."SLORD#"
     AND EO.RELEASE_NUMBER = L."SLREL#"
     AND EO.INVOICE_NUMBER = L.SLINV#
    LEFT JOIN GSFL2K.ITEMMAST IM
      ON L.SLITEM = IM.IMITEM
    LEFT JOIN GSFL2K.ITEMXTRA IMX
      ON IM.IMITEM = IMX.IMXITM
    GROUP BY
        L.SLCO,
        L.SLLOC,
        L."SLORD#",
        L."SLREL#",
        L.SLINV#
),

PrimaryContactRows AS (
    SELECT
        TRIM(CCNX.CCNXACCT) AS ACCT,
        TRIM(CNC.CNCFNAME) AS FNAME,
        TRIM(CNC.CNCLNAME) AS LNAME,
        CASE
            WHEN CNC.CNCPHONE IN (0, 555, 999) THEN NULL
            ELSE CNC.CNCPHONE
        END AS CONTACT_PHONE,
        CASE
            WHEN CNC.CNCCELL IN (0, 555, 999) THEN NULL
            ELSE CNC.CNCCELL
        END AS CONTACT_CELL,
        LOWER(TRIM(CNC.CNCE_MAIL)) AS CONTACT_EMAIL,
        ROW_NUMBER() OVER (
            PARTITION BY TRIM(CCNX.CCNXACCT)
            ORDER BY CNC.CNCCONTID
        ) AS CONTACT_RANK
    FROM GSFL2K.CONTXREF CCNX
    JOIN (
        SELECT DISTINCT ACCT
        FROM EligibleOrders
    ) EA
      ON EA.ACCT = TRIM(CCNX.CCNXACCT)
    JOIN GSFL2K.CONTCONT CNC
      ON CCNX.CCNXCONTID = CNC.CNCCONTID
    WHERE CNC.CNCPRIMARY = 'Y'
),

PrimaryContacts AS (
    SELECT
        ACCT,
        FNAME,
        LNAME,
        CONTACT_PHONE,
        CONTACT_CELL,
        CONTACT_EMAIL
    FROM PrimaryContactRows
    WHERE CONTACT_RANK = 1
),

CollectionSalesContacts AS (
    SELECT
        TRIM(IMX.IMCOLLECT) AS COLLECTION,
        TRIM(IM.IMDESC) AS DESCRIPTION,
        TRIM(SL.SLITEM) AS ITEM,
        SL.SLDATE AS SALE_DATE,
        SUM(SL.SLBLUS) AS SQFT_SOLD,
        SL."SLORD#" AS "ORDER#",
        TRIM(SL.SLINV#) AS "INVOICE#",
        TRIM(SH.SHCUST) AS ACCT,
        TRIM(SH.SHSTNM) AS ACCT_NAME,
        TRIM(SH.SHSTA1) AS ADDRESS_1,
        TRIM(SH.SHSTA2) AS ADDRESS_2,
        CASE
            WHEN LOCATE('  ', TRIM(SH.SHSTA3)) > 0
                THEN LEFT(TRIM(SH.SHSTA3), LOCATE('  ', TRIM(SH.SHSTA3)) - 1)
            ELSE TRIM(SH.SHSTA3)
        END AS CITY,
        RIGHT(TRIM(SH.SHSTA3), 2) AS STATE,
        EO.SHIP_ZIP5,
        EO.ACCOUNT_ZIP5,
        EO.ZIP_MATCH_SOURCE,
        EO.CUSTOMER_CLASS,
        PC.FNAME,
        PC.LNAME,
        PC.CONTACT_PHONE,
        PC.CONTACT_CELL,
        PC.CONTACT_EMAIL
    FROM EligibleOrders EO
    JOIN GSFL2K.SHLINE SL
      ON EO.COMPANY = SL.SLCO
     AND EO.LOCATION = SL.SLLOC
     AND EO.ORDER_NUMBER = SL."SLORD#"
     AND EO.RELEASE_NUMBER = SL."SLREL#"
     AND EO.INVOICE_NUMBER = SL.SLINV#
    JOIN GSFL2K.SHHEAD SH
      ON SL.SLCO = SH.SHCO
     AND SL.SLLOC = SH.SHLOC
     AND SL."SLORD#" = SH.SHORD#
     AND SL.SLINV# = SH.SHINV#
    LEFT JOIN OrderDivision1Mix D1
      ON D1.COMPANY = SL.SLCO
     AND D1.LOCATION = SL.SLLOC
     AND D1.ORDER_NUMBER = SL."SLORD#"
     AND D1.RELEASE_NUMBER = SL."SLREL#"
     AND D1.INVOICE_NUMBER = SL.SLINV#
    LEFT JOIN GSFL2K.ITEMMAST IM
      ON SL.SLITEM = IM.IMITEM
    LEFT JOIN GSFL2K.ITEMXTRA IMX
      ON IM.IMITEM = IMX.IMXITM
    LEFT JOIN PrimaryContacts PC
      ON PC.ACCT = EO.ACCT
    WHERE SL.SLFMCD IN ('EN', 'PF', 'SF', 'DF', 'VI', 'VF', 'SP', 'LA', 'RF', 'CF')
      AND TRIM(IM.IMITEM) <> 'GNTEST'
      AND TRIM(IM.IMFMCD) NOT IN ('MO')
      AND TRIM(IM.IMDESC) NOT LIKE '%SAMPLE%'
      AND TRIM(IM.IMDESC) NOT LIKE 'DISC%NUED'
      AND TRIM(IM.IMDESC) NOT LIKE '%SPECIAL%'
      AND TRIM(IM.IMDESC) NOT LIKE '%DO NOT USE%'
      AND TRIM(IM.IMDESC) NOT IN ('.')
      AND NOT (
          COALESCE(D1.DIVISION_1_LINE_COUNT, 0) > 0
          AND COALESCE(D1.NON_UNF_DIVISION_1_LINE_COUNT, 0) = 0
      )
    GROUP BY
        TRIM(IMX.IMCOLLECT),
        TRIM(IM.IMDESC),
        TRIM(SL.SLITEM),
        SL.SLDATE,
        SL."SLORD#",
        TRIM(SL.SLINV#),
        TRIM(SH.SHCUST),
        TRIM(SH.SHSTNM),
        TRIM(SH.SHSTA1),
        TRIM(SH.SHSTA2),
        CASE
            WHEN LOCATE('  ', TRIM(SH.SHSTA3)) > 0
                THEN LEFT(TRIM(SH.SHSTA3), LOCATE('  ', TRIM(SH.SHSTA3)) - 1)
            ELSE TRIM(SH.SHSTA3)
        END,
        RIGHT(TRIM(SH.SHSTA3), 2),
        EO.SHIP_ZIP5,
        EO.ACCOUNT_ZIP5,
        EO.ZIP_MATCH_SOURCE,
        EO.CUSTOMER_CLASS,
        PC.FNAME,
        PC.LNAME,
        PC.CONTACT_PHONE,
        PC.CONTACT_CELL,
        PC.CONTACT_EMAIL
)

SELECT *
FROM CollectionSalesContacts
WHERE SQFT_SOLD > 0
ORDER BY
    COLLECTION,
    ITEM,
    SALE_DATE DESC
