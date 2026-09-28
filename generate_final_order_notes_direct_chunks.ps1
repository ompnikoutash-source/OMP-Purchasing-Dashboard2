$ErrorActionPreference = 'Stop'

$sourcePath = 'H:\2025\MISC Reports\For Marketing\final orders.xlsx'
$chunkDir = Join-Path (Get-Location) 'final_order_notes_direct_chunks'
if (-not (Test-Path -LiteralPath $chunkDir)) {
    New-Item -ItemType Directory -Path $chunkDir | Out-Null
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-EntryText($zip, [string]$name) {
    $entry = $zip.GetEntry($name)
    if (-not $entry) { return $null }
    $stream = $entry.Open()
    try {
        $reader = [System.IO.StreamReader]::new($stream)
        try {
            return $reader.ReadToEnd()
        }
        finally {
            $reader.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function ColToIndex([string]$ref) {
    if (-not $ref) { return 0 }
    $letters = ([regex]::Match($ref, '^[A-Z]+')).Value
    if (-not $letters) { return 0 }
    $idx = 0
    foreach ($ch in $letters.ToCharArray()) {
        $idx = $idx * 26 + ([int][char]$ch - [int][char]'A' + 1)
    }
    return $idx
}

$fs = [System.IO.File]::Open(
    $sourcePath,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::ReadWrite
)
$zip = [System.IO.Compression.ZipArchive]::new($fs, [System.IO.Compression.ZipArchiveMode]::Read)

try {
    $shared = @()
    $sstText = Get-EntryText $zip 'xl/sharedStrings.xml'
    if ($sstText) {
        [xml]$sst = $sstText
        foreach ($si in $sst.SelectNodes("//*[local-name()='si']")) {
            $parts = @()
            foreach ($t in $si.SelectNodes(".//*[local-name()='t']")) {
                $parts += $t.InnerText
            }
            $shared += ($parts -join '')
        }
    }

    [xml]$ws = Get-EntryText $zip 'xl/worksheets/sheet1.xml'
    $orders = New-Object System.Collections.Generic.List[string]
    foreach ($row in $ws.SelectNodes("//*[local-name()='sheetData']/*[local-name()='row']")) {
        $rowNumText = $row.GetAttribute('r')
        if ([int]$rowNumText -eq 1) { continue }

        foreach ($c in $row.SelectNodes("./*[local-name()='c']")) {
            $ref = $c.GetAttribute('r')
            if ((ColToIndex $ref) -ne 2) { continue }

            $type = $c.GetAttribute('t')
            $vNode = $c.SelectSingleNode("./*[local-name()='v']")
            $value = ''
            if ($type -eq 's' -and $vNode) {
                $value = $shared[[int]$vNode.InnerText]
            }
            elseif ($vNode) {
                $value = $vNode.InnerText
            }

            $value = ([string]$value).Trim()
            if ($value -match '^\d+$' -and -not $orders.Contains($value)) {
                $orders.Add($value)
            }
        }
    }

    $chunkSize = 500
    $totalChunks = [int][Math]::Ceiling($orders.Count / $chunkSize)
    for ($chunkIndex = 0; $chunkIndex -lt $totalChunks; $chunkIndex++) {
        $start = $chunkIndex * $chunkSize
        $end = [Math]::Min($start + $chunkSize - 1, $orders.Count - 1)
        $chunkOrders = @($orders[$start..$end])
        $inList = ($chunkOrders -join ', ')
        $fileName = 'final_order_notes_direct_chunk_{0:00}_of_{1:00}.sql' -f ($chunkIndex + 1), $totalChunks
        $filePath = Join-Path $chunkDir $fileName

        $sql = @"
/*
   Standalone ODBC-safe chunk $($chunkIndex + 1) of $totalChunks.
   Generated from final orders.xlsx.
   Orders in this chunk: $($chunkOrders.Count)

   Returns one row per note line for orders that have notes.
   Run each chunk separately if your ODBC tool rejects large statements or temp-table scripts.
*/

WITH NOTE_LINES AS (
    SELECT
        T.STORD# AS ORDER_NUMBER,
        1 AS SOURCE_SORT,
        CAST('SHTEXT' AS VARCHAR(10)) AS NOTE_SOURCE,
        T.STSEQ# AS NOTE_SEQUENCE,
        RRN(T) AS NOTE_RRN,
        TRIM(COALESCE(T.STCMT1, '')) AS NOTE_LINE_1,
        TRIM(COALESCE(T.STCMT2, '')) AS NOTE_LINE_2,
        CAST(
            TRIM(COALESCE(T.STCMT1, '')) ||
            CASE
                WHEN TRIM(COALESCE(T.STCMT2, '')) <> ''
                THEN ' ' || TRIM(COALESCE(T.STCMT2, ''))
                ELSE ''
            END
            AS VARCHAR(500)
        ) AS NOTE_TEXT
    FROM GSFL2K.SHTEXT T
    WHERE T.STORD# IN ($inList)
      AND (TRIM(COALESCE(T.STCMT1, '')) <> '' OR TRIM(COALESCE(T.STCMT2, '')) <> '')

    UNION ALL

    SELECT
        T.OTORD# AS ORDER_NUMBER,
        2 AS SOURCE_SORT,
        CAST('OOTEXT' AS VARCHAR(10)) AS NOTE_SOURCE,
        T.OTSEQ# AS NOTE_SEQUENCE,
        RRN(T) AS NOTE_RRN,
        TRIM(COALESCE(T.OTCMT1, '')) AS NOTE_LINE_1,
        TRIM(COALESCE(T.OTCMT2, '')) AS NOTE_LINE_2,
        CAST(
            TRIM(COALESCE(T.OTCMT1, '')) ||
            CASE
                WHEN TRIM(COALESCE(T.OTCMT2, '')) <> ''
                THEN ' ' || TRIM(COALESCE(T.OTCMT2, ''))
                ELSE ''
            END
            AS VARCHAR(500)
        ) AS NOTE_TEXT
    FROM GSFL2K.OOTEXT T
    WHERE T.OTORD# IN ($inList)
      AND (TRIM(COALESCE(T.OTCMT1, '')) <> '' OR TRIM(COALESCE(T.OTCMT2, '')) <> '')
)
SELECT
    TRIM(CHAR(ORDER_NUMBER)) AS ORDER_NUMBER,
    NOTE_SOURCE,
    NOTE_SEQUENCE,
    NOTE_RRN,
    NOTE_LINE_1,
    NOTE_LINE_2,
    NOTE_TEXT
FROM NOTE_LINES
ORDER BY
    ORDER_NUMBER,
    SOURCE_SORT,
    NOTE_SEQUENCE,
    NOTE_RRN;
"@
        [System.IO.File]::WriteAllText($filePath, $sql, [System.Text.Encoding]::ASCII)
    }

    Get-ChildItem -LiteralPath $chunkDir -Filter '*.sql' |
        Sort-Object Name |
        Select-Object Name, Length, LastWriteTime
}
finally {
    $zip.Dispose()
    $fs.Dispose()
}
