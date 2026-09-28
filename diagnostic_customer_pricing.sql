/* ============================================================
   DIAGNOSTIC — Customer Pricing Table Discovery
   Paste ONE block at a time into Power Query.
   ============================================================ */


/* ── STEP 1 — Run this first ─────────────────────────────────
   Lists tables in GSFL2K with PRIC or PRICE in the name.
   Send back the results.
   ──────────────────────────────────────────────────────────── */

SELECT
    TABLE_NAME,
    TABLE_TEXT
FROM QSYS2.SYSTABLES
WHERE TABLE_SCHEMA = 'GSFL2K'
  AND TABLE_NAME LIKE '%PRIC%'
ORDER BY TABLE_NAME


/* ── STEP 2 — Run after STEP 1 ───────────────────────────────
   Replace YOURTABLENAME with the table name from STEP 1.
   Lists every column and its description for that table.
   ──────────────────────────────────────────────────────────── */

SELECT
    COLUMN_NAME,
    COLUMN_TEXT,
    DATA_TYPE,
    LENGTH,
    ORDINAL_POSITION
FROM QSYS2.SYSCOLUMNS
WHERE TABLE_SCHEMA = 'GSFL2K'
  AND TABLE_NAME   = 'YOURTABLENAME'
ORDER BY ORDINAL_POSITION


/* ── STEP 3 — Run after STEP 2 ───────────────────────────────
   Replace YOURTABLENAME and YOURCUSTFIELD with real values.
   Pulls sample rows for CEN100 to see what the data looks like.
   ──────────────────────────────────────────────────────────── */

SELECT *
FROM GSFL2K.YOURTABLENAME
WHERE TRIM(YOURCUSTFIELD) = 'CEN100'
FETCH FIRST 20 ROWS ONLY
