/* ============================================================
   DIAGNOSTIC: MOBSPR300BFJ — date-window comparison
   Compares two sales windows to identify which one aligns
   with the AS/400 IM445 report values (run 4/3/2026):
     Window A: 2025-03-01 → 2026-03-30  (12-completed-months logic)
     Window B: 2025-04-03 → 2026-04-03  (trailing 365 days)

   Shows companywide totals AND loc 9 split for each window.
   AS/400 report reference values: Revenue $11,655.68 | Units 40,192
   ============================================================ */

SELECT
    WINDOW,
    DECIMAL(REVENUE_ALL,   18, 2)  AS "Revenue - All Locs",
    DECIMAL(UNITS_ALL,     18, 2)  AS "Units - All Locs",
    ORDERS_ALL                     AS "Orders - All Locs",
    DECIMAL(REVENUE_LOC9,  18, 2)  AS "Revenue - Loc 9",
    DECIMAL(UNITS_LOC9,    18, 2)  AS "Units - Loc 9",
    ORDERS_LOC9                    AS "Orders - Loc 9"
FROM (

    -- ── Window A: 2025-03-01 → 2026-03-30 ──────────────────────────────────
    SELECT
        'A: 2025-03-01 to 2026-03-30'                                            AS WINDOW,
        SUM(COALESCE(L.SLENET, 0))                                               AS REVENUE_ALL,
        SUM(COALESCE(L.SLBLUO, 0))                                               AS UNITS_ALL,
        COUNT(DISTINCT RTRIM(CHAR(H.SHCO)) || '|'
            || RTRIM(CHAR(H.SHLOC)) || '|'
            || RTRIM(CHAR(H.SHINV#)))                                            AS ORDERS_ALL,
        SUM(CASE WHEN H.SHLOC = 9 THEN COALESCE(L.SLENET, 0) ELSE 0 END)       AS REVENUE_LOC9,
        SUM(CASE WHEN H.SHLOC = 9 THEN COALESCE(L.SLBLUO, 0) ELSE 0 END)       AS UNITS_LOC9,
        COUNT(DISTINCT CASE WHEN H.SHLOC = 9
            THEN RTRIM(CHAR(H.SHCO)) || '|'
                || RTRIM(CHAR(H.SHLOC)) || '|'
                || RTRIM(CHAR(H.SHINV#)) END)                                    AS ORDERS_LOC9
    FROM GSFL2K.SHHEAD H
    JOIN GSFL2K.SHLINE L
      ON  L.SLCO   = H.SHCO
      AND L.SLLOC  = H.SHLOC
      AND L.SLINV# = H.SHINV#
      AND L.SLORD# = H.SHORD#
    WHERE TRIM(L.SLITEM) = 'MOBSPR300BFJ'
      AND H.SHIDAT >= DATE('2025-03-01')
      AND H.SHIDAT <= DATE('2026-03-30')
      AND H.SHLOC  IN (1, 3, 4, 5, 6, 7, 8, 9)

    UNION ALL

    -- ── Window B: 2025-04-03 → 2026-04-03 (trailing 365 days) ──────────────
    SELECT
        'B: 2025-04-03 to 2026-04-03'                                            AS WINDOW,
        SUM(COALESCE(L.SLENET, 0))                                               AS REVENUE_ALL,
        SUM(COALESCE(L.SLBLUO, 0))                                               AS UNITS_ALL,
        COUNT(DISTINCT RTRIM(CHAR(H.SHCO)) || '|'
            || RTRIM(CHAR(H.SHLOC)) || '|'
            || RTRIM(CHAR(H.SHINV#)))                                            AS ORDERS_ALL,
        SUM(CASE WHEN H.SHLOC = 9 THEN COALESCE(L.SLENET, 0) ELSE 0 END)       AS REVENUE_LOC9,
        SUM(CASE WHEN H.SHLOC = 9 THEN COALESCE(L.SLBLUO, 0) ELSE 0 END)       AS UNITS_LOC9,
        COUNT(DISTINCT CASE WHEN H.SHLOC = 9
            THEN RTRIM(CHAR(H.SHCO)) || '|'
                || RTRIM(CHAR(H.SHLOC)) || '|'
                || RTRIM(CHAR(H.SHINV#)) END)                                    AS ORDERS_LOC9
    FROM GSFL2K.SHHEAD H
    JOIN GSFL2K.SHLINE L
      ON  L.SLCO   = H.SHCO
      AND L.SLLOC  = H.SHLOC
      AND L.SLINV# = H.SHINV#
      AND L.SLORD# = H.SHORD#
    WHERE TRIM(L.SLITEM) = 'MOBSPR300BFJ'
      AND H.SHIDAT >= DATE('2025-04-03')
      AND H.SHIDAT <= DATE('2026-04-03')
      AND H.SHLOC  IN (1, 3, 4, 5, 6, 7, 8, 9)

) WINDOWS

FOR READ ONLY
