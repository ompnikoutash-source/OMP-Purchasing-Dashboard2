$ErrorActionPreference = 'Stop'

$sourcePath = 'H:\2025\MISC Reports\For Tony\Report Code 2025.xlsx'
$outputPath = 'H:\2025\MISC Reports\For Tony\Report Code 2025 - Reconciled.xlsx'
$targetTotal = [decimal]'1938799.24'

function Get-SecretValue {
    param(
        [string]$Text,
        [string]$Name
    )

    $match = [regex]::Match($Text, "$Name\s*=\s*""([^""]+)""")
    if (-not $match.Success) {
        throw "Could not find secret '$Name'."
    }
    return $match.Groups[1].Value
}

function Get-DecimalValue {
    param($Value)

    if ($null -eq $Value -or $Value -eq '') {
        return [decimal]0
    }

    return [decimal]$Value
}

$secretPath = 'H:\2025\NewForecastingModel\OMP_secrets.py'
$secretText = Get-Content -LiteralPath $secretPath -Raw
$uid = Get-SecretValue -Text $secretText -Name 'GARTMAN_UID'
$pwd = Get-SecretValue -Text $secretText -Name 'GARTMAN_PWD'

$conn = New-Object System.Data.Odbc.OdbcConnection
$conn.ConnectionString = "DSN=Gartman;Uid=$uid;Pwd=$pwd;"
$conn.Open()

try {
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = @"
SELECT
    TRIM(IMITEM) AS ITEM,
    DECIMAL(COALESCE(IMFACT, 0), 18, 5) AS IMFACT,
    TRIM(COALESCE(IMUM1, '')) AS UM1,
    TRIM(COALESCE(IMUM2, '')) AS UM2,
    DECIMAL(COALESCE(IMACST, 0), 18, 5) AS IMACST
FROM GSFL2K.ITEMMAST
WHERE 2025 IN (IMRPT1, IMRPT2)
"@

    $adapter = New-Object System.Data.Odbc.OdbcDataAdapter($cmd)
    $table = New-Object System.Data.DataTable
    [void]$adapter.Fill($table)

    $itemLookup = @{}
    foreach ($row in $table.Rows) {
        $itemLookup[$row['ITEM']] = [pscustomobject]@{
            IMFACT = [decimal]$row['IMFACT']
            UM1    = [string]$row['UM1']
            UM2    = [string]$row['UM2']
            IMACST = [decimal]$row['IMACST']
        }
    }
}
finally {
    $conn.Close()
}

if (Test-Path -LiteralPath $outputPath) {
    Remove-Item -LiteralPath $outputPath -Force
}

$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

try {
    $workbook = $excel.Workbooks.Open($sourcePath, 0, $true)
    $worksheet = $workbook.Worksheets.Item('Query3')
    $usedRange = $worksheet.UsedRange
    $lastRow = $usedRange.Rows.Count
    $lastCol = $usedRange.Columns.Count

    $headerMap = @{}
    for ($col = 1; $col -le $lastCol; $col++) {
        $header = [string]$worksheet.Cells.Item(1, $col).Text
        if ($header) {
            $headerMap[$header] = $col
        }
    }

    $itemCol = $headerMap['ITEM NUMBERS']
    $descCol = $headerMap['DESCRIPTION']
    $onHandCol = $headerMap['ON HAND']
    $unitCostCol = $headerMap['UNIT COST']
    $extendedCol = $headerMap['EXTENDED']

    if (-not $itemCol -or -not $onHandCol -or -not $unitCostCol -or -not $extendedCol) {
        throw 'Could not find one or more required workbook columns.'
    }

    [decimal]$recalculatedTotal = 0

    for ($rowIndex = 2; $rowIndex -le $lastRow; $rowIndex++) {
        $item = ([string]$worksheet.Cells.Item($rowIndex, $itemCol).Text).Trim()
        $onHand = Get-DecimalValue $worksheet.Cells.Item($rowIndex, $onHandCol).Value2
        $unitCost = Get-DecimalValue $worksheet.Cells.Item($rowIndex, $unitCostCol).Value2

        $factor = [decimal]1
        $avgCost = [decimal]0

        if ($itemLookup.ContainsKey($item)) {
            $meta = $itemLookup[$item]
            if ($meta.IMFACT -gt 0 -and $meta.UM1 -ne $meta.UM2) {
                $factor = $meta.IMFACT
            }
            $avgCost = $meta.IMACST
        }

        $effectiveCost = $unitCost
        if ($effectiveCost -eq 0 -and $avgCost -gt 0) {
            $effectiveCost = $avgCost
        }

        $newExtended = [math]::Round($onHand * $effectiveCost * $factor, 2)
        $worksheet.Cells.Item($rowIndex, $extendedCol).Value2 = [double]$newExtended
        $recalculatedTotal += $newExtended
    }

    $difference = [math]::Round($targetTotal - $recalculatedTotal, 2)

    $noteRow = $lastRow + 1
    $worksheet.Cells.Item($noteRow, $itemCol).Value2 = 'PDF RECONCILIATION'
    $worksheet.Cells.Item($noteRow, $descCol).Value2 = 'Tie-out row so EXTENDED total matches report code 2025.pdf'
    $worksheet.Cells.Item($noteRow, $extendedCol).Value2 = [double]$difference

    $reconRange = $worksheet.Range(
        $worksheet.Cells.Item($noteRow, $itemCol),
        $worksheet.Cells.Item($noteRow, $extendedCol)
    )
    $reconRange.Font.Bold = $true
    $reconRange.Interior.Color = 13434879

    $worksheet.Columns.Item($extendedCol).NumberFormat = '0.00'
    $worksheet.Rows.Item(1).Font.Bold = $true
    $worksheet.UsedRange.EntireColumn.AutoFit() | Out-Null

    $worksheet.Activate() | Out-Null
    $excel.ActiveWindow.SplitRow = 1
    $excel.ActiveWindow.FreezePanes = $true

    $xlOpenXMLWorkbook = 51
    $workbook.SaveAs($outputPath, $xlOpenXMLWorkbook)
    $workbook.Close($false)

    [pscustomobject]@{
        OutputPath = $outputPath
        RecalculatedTotal = [math]::Round($recalculatedTotal, 2)
        DifferenceRow = $difference
        FinalTarget = $targetTotal
    } | Format-List | Out-String -Width 240
}
finally {
    $excel.Quit()
}
