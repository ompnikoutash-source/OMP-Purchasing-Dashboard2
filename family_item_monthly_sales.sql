-- monthly sqft and dollar sales by item, with collection and color, for a given flooring family
-- family codes: Hardwood -> EN, PF, SF, DF | Vinyl -> VI, VF, SP, RF, CF | Laminate -> LA
-- active-item filter: excludes items flagged Dropped (IMDROP='D') and requires a received
-- PO (real physical arrival, IRSRC='P') within 24 months, so old/discontinued/dropped colors
-- that still carry historical sales rows don't pollute a "current catalog" analysis
SELECT
    TRIM(SL.SLITEM) AS ITEM,
    TRIM(IM.IMDESC) AS DESCRIPTION,
    TRIM(IM.IMCOLR) AS COLOR,
    TRIM(IMX.IMCOLLECT) AS COLLECTION,
    TRIM(IM.IMFMCD) AS FAMILY_CODE,
    YEAR(SL.SLDATE) AS SALE_YEAR,
    MONTH(SL.SLDATE) AS SALE_MONTH,
    SUM(SL.SLBLUS) AS SQFT_SOLD,
    ROUND(SUM(SL.SLBLUS * SL.SLPRIC), 2) AS SALES_DOLLARS
FROM GSFL2K.SHLINE SL
LEFT JOIN GSFL2K.ITEMMAST IM
    ON SL.SLITEM = IM.IMITEM
LEFT JOIN GSFL2K.ITEMXTRA IMX
    ON IM.IMITEM = IMX.IMXITM
WHERE
    IM.IMFMCD IN ('EN') AND
    SL.SLDATE BETWEEN CURRENT DATE - 36 MONTHS AND CURRENT DATE AND
    TRIM(IM.IMDESC) NOT LIKE '%SAMPLE%' AND
    TRIM(IM.IMDESC) NOT LIKE '%SPECIAL%' AND
    TRIM(IM.IMDESC) NOT LIKE '%CUSTOM%' AND
    TRIM(IM.IMDESC) NOT LIKE 'DISC%NUED' AND
    TRIM(IM.IMDESC) NOT LIKE '%DO NOT USE%' AND
    TRIM(IM.IMITEM) != 'GNTEST' AND
    IM.IMDROP <> 'D' AND
    EXISTS (
        SELECT 1 FROM GSFL2K.ITEMRECH IR
        WHERE IR.IRITEM = IM.IMITEM
          AND IR.IRSRC = 'P'
          AND IR.IRDATE >= CURRENT DATE - 24 MONTHS
    )
GROUP BY
    TRIM(SL.SLITEM),
    TRIM(IM.IMDESC),
    TRIM(IM.IMCOLR),
    TRIM(IMX.IMCOLLECT),
    TRIM(IM.IMFMCD),
    YEAR(SL.SLDATE),
    MONTH(SL.SLDATE)
ORDER BY
    ITEM,
    SALE_YEAR,
    SALE_MONTH