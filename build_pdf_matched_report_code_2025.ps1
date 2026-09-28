$ErrorActionPreference = 'Stop'

$pdfWorkbookPath = 'H:\2025\MISC Reports\For Tony\Report Code 2025 - PDF Exact.xlsx'
$sourceWorkbookPath = 'H:\2025\MISC Reports\For Tony\Report Code 2025.xlsx'
$outputWorkbookPath = 'H:\2025\MISC Reports\For Tony\Report Code 2025 - PDF Row Matched.xlsx'
$targetTotal = [decimal]'1938799.24'
$costTypes = @('A', 'B', 'C', 'D', 'N')

function Parse-DecimalText {
    param([string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return $null
    }

    $clean = $Text.Trim() -replace ',', ''
    try {
        return [decimal]::Parse($clean, [System.Globalization.CultureInfo]::InvariantCulture)
    }
    catch {
        return $null
    }
}

function Normalize-Text {
    param([string]$Text)

    if ($null -eq $Text) {
        return ''
    }

    return ($Text.Trim() -replace '\s+', ' ')
}

function Normalize-LotSelect {
    param([string]$Text)

    $parsed = Parse-DecimalText $Text
    if ($null -ne $parsed) {
        return ('{0:F2}' -f $parsed)
    }

    return (Normalize-Text $Text).ToUpperInvariant()
}

function Round-Currency {
    param([decimal]$Value)

    return [decimal]::Round($Value, 2, [System.MidpointRounding]::AwayFromZero)
}

function Make-Key {
    param(
        [string]$Item,
        [string]$Loc,
        [string]$Serial,
        [string]$Bin,
        [decimal]$UnitCost
    )

    return @(
        (Normalize-Text $Item).ToUpperInvariant(),
        (Normalize-Text $Loc).ToUpperInvariant(),
        (Normalize-Text $Serial).ToUpperInvariant(),
        (Normalize-Text $Bin).ToUpperInvariant(),
        ('{0:F5}' -f $UnitCost)
    ) -join '|'
}

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-XlsxSharedStrings {
    param($Zip)

    $shared = @()
    $entry = $Zip.GetEntry('xl/sharedStrings.xml')
    if (-not $entry) {
        return $shared
    }

    [xml]$xml = New-Object xml
    $reader = New-Object IO.StreamReader($entry.Open())
    try {
        $xml.LoadXml($reader.ReadToEnd())
    }
    finally {
        $reader.Close()
    }

    foreach ($si in $xml.sst.si) {
        if ($si.t) {
            $shared += [string]$si.t
        }
        elseif ($si.r) {
            $shared += (($si.r | ForEach-Object { $_.t.'#text' }) -join '')
        }
        else {
            $shared += ''
        }
    }

    return $shared
}

function Get-XlsxCellText {
    param($Cell, $SharedStrings)

    $v = $Cell.v
    if ($null -eq $v) {
        return ''
    }

    if ($Cell.t -eq 's') {
        return [string]$SharedStrings[[int]$v]
    }

    return [string]$v
}

function Get-RowMap {
    param($RowNode, $SharedStrings)

    $map = @{}
    foreach ($cell in $RowNode.c) {
        $ref = [string]$cell.r
        $col = ($ref -replace '\d', '')
        $map[$col] = Normalize-Text (Get-XlsxCellText -Cell $cell -SharedStrings $SharedStrings)
    }
    return $map
}

$tmpPdfWorkbookPath = 'H:\2025\NewForecastingModel\OMPforecasting5\_pdf_exact_tmp.xlsx'
Copy-Item -LiteralPath $pdfWorkbookPath -Destination $tmpPdfWorkbookPath -Force

$zip = [System.IO.Compression.ZipFile]::OpenRead($tmpPdfWorkbookPath)
try {
    $sharedStrings = Get-XlsxSharedStrings -Zip $zip
    $sheetEntry = $zip.GetEntry('xl/worksheets/sheet1.xml')
    [xml]$sheetXml = New-Object xml
    $reader = New-Object IO.StreamReader($sheetEntry.Open())
    try {
        $sheetXml.LoadXml($reader.ReadToEnd())
    }
    finally {
        $reader.Close()
    }

    $pdfRows = New-Object System.Collections.Generic.List[object]
    $currentItem = ''
    $currentDesc = ''
    $lastRecord = $null

    foreach ($rowNode in $sheetXml.worksheet.sheetData.row) {
        $rowMap = Get-RowMap -RowNode $rowNode -SharedStrings $sharedStrings
        $b = $rowMap['B']
        $c = $rowMap['C']
        $d = $rowMap['D']
        $e = $rowMap['E']
        $f = $rowMap['F']
        $g = $rowMap['G']
        $h = $rowMap['H']
        $i = $rowMap['I']
        $j = $rowMap['J']
        $k = $rowMap['K']

        $nonPage = @(@($b,$c,$d,$e,$f,$g,$h,$i,$j,$k) | Where-Object { $_ })
        if ($nonPage.Count -eq 0) {
            continue
        }

        if ($b -eq 'IM150' -or $b -eq '**GRAND TOTAL' -or $e -like 'ITEM*') {
            continue
        }

        $cells = @($b,$c,$d,$e,$f,$g,$h,$i,$j,$k)

        $locSerialIndex = -1
        for ($idx = 0; $idx -lt $cells.Count; $idx++) {
            if ($cells[$idx] -match '^\d{3}/\d{2}(?:\s+.+)?$') {
                $locSerialIndex = $idx
                break
            }
        }

        $costTypeIndex = -1
        for ($idx = 0; $idx -lt $cells.Count; $idx++) {
            if ($cells[$idx] -in $costTypes) {
                $costTypeIndex = $idx
                break
            }
        }

        $inlineHeaderText = ''
        if (-not $b) {
            if ($d -match '^[A-Z0-9][A-Z0-9''/#&().-]*\s+.+') {
                $inlineHeaderText = $d
            }
            elseif ($e -match '^[A-Z0-9][A-Z0-9''/#&().-]*\s+.+') {
                $inlineHeaderText = $e
            }
        }

        $isItemHeader = ($b -or $inlineHeaderText) -and ($locSerialIndex -lt 0) -and ($costTypeIndex -lt 0)
        if ($isItemHeader) {
            if ($b) {
                $currentItem = $b
                $descParts = @($c, $d, $e, $f) | Where-Object { $_ }
                $currentDesc = ($descParts -join ' ').Trim()
            }
            else {
                $headerParts = $inlineHeaderText.Split(' ', 2, [System.StringSplitOptions]::RemoveEmptyEntries)
                $currentItem = $headerParts[0]
                $currentDesc = if ($headerParts.Count -ge 2) { $headerParts[1] } else { '' }
            }
            continue
        }

        if ($locSerialIndex -ge 0) {
            $locSerial = $cells[$locSerialIndex]
            $parts = $locSerial.Split(' ', 2, [System.StringSplitOptions]::RemoveEmptyEntries)

            $bin = ''
            $startIndex = $locSerialIndex + 1
            $scanLimit = if ($costTypeIndex -gt $locSerialIndex) { $costTypeIndex } else { $cells.Count }
            for ($idx = $startIndex; $idx -lt $scanLimit; $idx++) {
                $candidate = $cells[$idx]
                if (-not $candidate) {
                    continue
                }

                $candidateNum = Parse-DecimalText $candidate
                if ($null -eq $candidateNum -and $candidate -notin $costTypes) {
                    $bin = $candidate
                    $startIndex = $idx + 1
                    break
                }

                if ($null -ne $candidateNum) {
                    $startIndex = $idx
                    break
                }
            }

            $numericIndices = New-Object System.Collections.Generic.List[int]
            for ($idx = $startIndex; $idx -lt $cells.Count; $idx++) {
                if ($null -ne (Parse-DecimalText $cells[$idx])) {
                    $numericIndices.Add($idx)
                }
            }

            if ($numericIndices.Count -lt 1) {
                continue
            }

            $onHand = Parse-DecimalText $cells[$numericIndices[0]]
            $unitCost = if ($costTypeIndex -gt $locSerialIndex -and ($costTypeIndex - 1) -ge 0) {
                Parse-DecimalText $cells[$costTypeIndex - 1]
            }
            elseif ($numericIndices.Count -ge 2) {
                Parse-DecimalText $cells[$numericIndices[$numericIndices.Count - 1]]
            }
            else {
                $null
            }

            $hasExplicitExtended = $false
            $extended = if ($costTypeIndex -gt $locSerialIndex) {
                $extendedIndices = @($numericIndices | Where-Object { $_ -gt $costTypeIndex })
                if ($extendedIndices.Count -gt 0) {
                    $hasExplicitExtended = $true
                    Parse-DecimalText $cells[$extendedIndices[$extendedIndices.Count - 1]]
                }
                else {
                    $null
                }
            }
            else {
                $null
            }

            if ($null -eq $onHand) {
                continue
            }

            if ($null -eq $unitCost) {
                $unitCost = [decimal]0
            }

            $locText = $parts[0]
            $serialText = if ($parts.Count -ge 2) { $parts[1] } else { '' }
            $locNum = $locText.Substring($locText.IndexOf('/') + 1).TrimStart('0')
            if (-not $locNum) { $locNum = '0' }

            $record = [pscustomobject]@{
                Item = $currentItem
                Description = $currentDesc
                Loc = $locNum
                Serial = $serialText
                LotSelect = ''
                Bin = $bin
                OnHand = $onHand
                UnitCost = $unitCost
                Extended = $extended
                HasExplicitExtended = $hasExplicitExtended
            }

            $pdfRows.Add($record)
            $lastRecord = $record
            continue
        }

        $isLotSelectRow = $lastRecord -and ($locSerialIndex -lt 0) -and ($costTypeIndex -lt 0) -and ($nonPage.Count -eq 1)
        if ($isLotSelectRow) {
            $lastRecord.LotSelect = $nonPage[0]
        }
    }
}
finally {
    $zip.Dispose()
}

[decimal]$parsedPdfExtendedTotal = 0
foreach ($row in $pdfRows) {
    if ($null -ne $row.Extended) {
        $parsedPdfExtendedTotal += $row.Extended
    }
}

$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

try {

    $lookup = @{}
    foreach ($row in $pdfRows) {
        $key = Make-Key -Item $row.Item -Loc $row.Loc -Serial $row.Serial -Bin $row.Bin -UnitCost $row.UnitCost
        if (-not $lookup.ContainsKey($key)) {
            $lookup[$key] = New-Object System.Collections.Queue
        }
        $lookup[$key].Enqueue($row)
    }

    if (Test-Path -LiteralPath $outputWorkbookPath) {
        Remove-Item -LiteralPath $outputWorkbookPath -Force
    }
    Copy-Item -LiteralPath $sourceWorkbookPath -Destination $outputWorkbookPath

    $targetWb = $excel.Workbooks.Open($outputWorkbookPath)
    $targetWs = $targetWb.Worksheets.Item('Query3')
    $targetUsed = $targetWs.UsedRange
    $targetValues = $targetUsed.Value2

    $headers = @{}
    for ($c = 1; $c -le $targetUsed.Columns.Count; $c++) {
        $h = Normalize-Text ([string]$targetValues[1, $c])
        if ($h) { $headers[$h] = $c }
    }

    $itemCol = $headers['ITEM NUMBERS']
    $locCol = $headers['LOC']
    $serialCol = $headers['SERIAL/LOT']
    $binCol = $headers['BIN']
    $onHandCol = $headers['ON HAND']
    $unitCostCol = $headers['UNIT COST']
    $extendedCol = $headers['EXTENDED']

    $matched = 0
    $unmatched = New-Object System.Collections.Generic.List[object]
    [decimal]$sum = 0

    for ($r = 2; $r -le $targetUsed.Rows.Count; $r++) {
        $item = Normalize-Text ([string]$targetValues[$r, $itemCol])
        $loc = Normalize-Text ([string]$targetValues[$r, $locCol])
        $serial = Normalize-Text ([string]$targetValues[$r, $serialCol])
        $bin = Normalize-Text ([string]$targetValues[$r, $binCol])
        $onHand = [decimal]$targetValues[$r, $onHandCol]
        $unitCost = [decimal]$targetValues[$r, $unitCostCol]

        $key = Make-Key -Item $item -Loc $loc -Serial $serial -Bin $bin -UnitCost $unitCost

        if ($lookup.ContainsKey($key) -and $lookup[$key].Count -gt 0) {
            $pdfRow = $lookup[$key].Dequeue()
            $extendedValue = if ($pdfRow.HasExplicitExtended -and $null -ne $pdfRow.Extended) {
                $pdfRow.Extended
            }
            else {
                $basisOnHand = if ([decimal]$pdfRow.OnHand -gt [decimal]$onHand) {
                    [decimal]$pdfRow.OnHand
                }
                else {
                    [decimal]$onHand
                }
                Round-Currency ($basisOnHand * [decimal]$unitCost)
            }

            $targetWs.Cells.Item($r, $extendedCol).Value2 = [double]$extendedValue
            $sum += $extendedValue
            $matched++
        }
        else {
            $unmatched.Add([pscustomobject]@{
                Row = $r
                Key = $key
                Item = $item
                Loc = $loc
                Serial = $serial
                Bin = $bin
                OnHand = $onHand
                UnitCost = $unitCost
            })
        }
    }

    $targetWs.Rows.Item(1).Font.Bold = $true
    $targetWs.UsedRange.EntireColumn.AutoFit() | Out-Null
    $targetWs.Activate() | Out-Null
    $excel.ActiveWindow.SplitRow = 1
    $excel.ActiveWindow.FreezePanes = $true

    $targetWb.Save()
    $targetWb.Close($false)

    $leftovers = 0
    foreach ($q in $lookup.Values) {
        $leftovers += $q.Count
    }

    [pscustomobject]@{
        OutputPath = $outputWorkbookPath
        ParsedPdfRows = $pdfRows.Count
        ParsedPdfExtendedTotal = [math]::Round($parsedPdfExtendedTotal, 2)
        MatchedWorkbookRows = $matched
        UnmatchedWorkbookRows = $unmatched.Count
        UnusedPdfRows = $leftovers
        ExtendedTotal = [math]::Round($sum, 2)
        TargetTotal = $targetTotal
    } | Format-List | Out-String -Width 260

    if ($unmatched.Count -gt 0) {
        '--- UNMATCHED SAMPLE ---'
        $unmatched | Select-Object -First 10 | Format-Table -AutoSize | Out-String -Width 320
    }
}
finally {
    $excel.Quit()
}
