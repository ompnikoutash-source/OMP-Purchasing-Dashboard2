param(
    [string]$InputPath = 'final_order_notes_results_summary.csv',
    [string]$OutputPath = 'final_order_notes_results_summary_parsed.csv',
    [string]$ReviewPath = 'final_order_notes_qty_shipping_review.csv'
)

$ErrorActionPreference = 'Stop'

function Normalize-Note([string]$Text) {
    if (-not $Text) { return '' }

    $normalized = $Text.ToUpperInvariant()
    $normalized = $normalized.Replace([char]0x00A0, ' ')
    $normalized = $normalized -replace '[“”]', '"'
    $normalized = $normalized -replace "[’`]", "'"
    $normalized = $normalized -replace '\bSMAPLE\b', 'SAMPLE'
    $normalized = $normalized -replace '\bSAMLPLE\b', 'SAMPLE'
    $normalized = $normalized -replace '\bSMPLE\b', 'SAMPLE'
    $normalized = $normalized -replace '\bSAMPLES\s+CHIPS\b', 'SAMPLE CHIPS'
    $normalized = $normalized -replace '\bCHIPES\b', 'CHIPS'

    $wordNumbers = @{
        ONE = 1; TWO = 2; THREE = 3; FOUR = 4; FIVE = 5; SIX = 6
        SEVEN = 7; EIGHT = 8; NINE = 9; TEN = 10; ELEVEN = 11; TWELVE = 12
    }
    foreach ($word in $wordNumbers.Keys) {
        $normalized = [regex]::Replace(
            $normalized,
            "\b$word\b(?=\s+(?:SAMPLE|SAMPLES|CHIP|CHIPS|PCS|PIECES))",
            [string]$wordNumbers[$word],
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }

    return ([regex]::Replace($normalized, '\s+', ' ')).Trim()
}

function Convert-ToDecimalString($Value) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return ''
    }
    $number = 0.0
    if ([double]::TryParse([string]$Value, [ref]$number)) {
        return ('{0:0.##}' -f $number)
    }
    return [string]$Value
}

function Get-DollarInfo([string]$RawText, [string]$NormalizedText) {
    $dollarMatches = [regex]::Matches($RawText, '\$\s*\d+(?:,\d{3})*(?:\.\d{1,2})?')
    $amounts = New-Object System.Collections.Generic.List[string]
    $shippingCandidates = New-Object System.Collections.Generic.List[object]

    $chargeWord = '(?:CHARGE|CHRAGE|CARGE|CHG)'
    $noCharge = $NormalizedText -match "\b(?:NO|MO|N/C)\s+(?:UPS\s+|FREIGHT\s+|DELIVERY\s+|DLV\s+)?$chargeWord\b" -or
        $NormalizedText -match "\bNO\s+$chargeWord\s+(?:FOR\s+)?(?:UPS|DLV|DELIVERY|FREIGHT|SAMPLES?|SAMPLE\s+CHIPS?)\b" -or
        $NormalizedText -match "\bNO\s+(?:UPS|FREIGHT|DELIVERY|DLV)\s+$chargeWord\b" -or
        $NormalizedText -match "\b(?:SHIP|UPS|GROUND|FREIGHT|DELIVERY|DLV|SAMPLES?|SAMPLE\s+CHIPS?).{0,40}\b(?:NO|MO)\s+$chargeWord\b"

    foreach ($match in $dollarMatches) {
        $rawAmount = $match.Value
        $numericText = ($rawAmount -replace '[^\d.]', '')
        $numeric = 0.0
        if (-not [double]::TryParse($numericText, [ref]$numeric)) { continue }
        $amounts.Add(('{0:0.00}' -f $numeric))

        $start = [Math]::Max(0, $match.Index - 55)
        $length = [Math]::Min($RawText.Length - $start, 125)
        $context = (Normalize-Note $RawText.Substring($start, $length))

        $amountPattern = '\$\s*\d+(?:,\d{3})*(?:\.\d{1,2})?'
        $contextHasNoCharge = $context -match "\b(?:NO|MO|N/C)\s+(?:UPS\s+|FREIGHT\s+|DELIVERY\s+|DLV\s+)?$chargeWord\b" -or
            $context -match "\bNO\s+$chargeWord\s+(?:FOR\s+)?(?:UPS|DLV|DELIVERY|FREIGHT|SAMPLES?|SAMPLE\s+CHIPS?)\b" -or
            $context -match "\bNO\s+(?:UPS|FREIGHT|DELIVERY|DLV)\s+$chargeWord\b" -or
            $context -match "\b(?:SHIP|UPS|GROUND|FREIGHT|DELIVERY|DLV|SAMPLES?|SAMPLE\s+CHIPS?).{0,40}\b(?:NO|MO)\s+$chargeWord\b"
        $hasExplicitShippingCharge = ($context -match "$amountPattern.{0,45}\b(?:DELIVERY|FREIGHT|SHIPPING)\s+(?:FEE|COST|$chargeWord)\b" -or
            $context -match "\b(?:DELIVERY|FREIGHT|SHIPPING)\s+(?:FEE|COST|$chargeWord)\b.{0,45}$amountPattern") -and
            -not $contextHasNoCharge
        $hasShippingQuoteContext = $context -match '\b(QUOTE|LBS|WEIGHT|WT|TRANSIT)\b'
        $hasPaymentWords = $context -match '\b(PAID|PIAD|PAOID|VISA|AMEX|MASTER|M/C|MC|CASH|CHECK|CHK|CK#|TOTAL|REFUND|CREDIT|CARD|ORDER#|MAT\s+FRM|MATERIAL|CTNS?|BOXES|BXS|S/F|SQFT|PRICE|REBATE|RESTOCK|RESTOCKING|PER\s+B\.?F|RAN\s+C/C|WIRE)\b'

        if (($hasExplicitShippingCharge -or $hasShippingQuoteContext) -and -not $hasPaymentWords) {
            $shippingCandidates.Add([pscustomobject]@{
                Amount = $numeric
                Rule = if ($hasShippingQuoteContext) { 'dollar_near_shipping_quote_context' } else { 'dollar_near_explicit_shipping_charge' }
                Context = $context
            })
        }
    }

    $review = New-Object System.Collections.Generic.List[string]
    $shippingCost = ''
    $shippingRule = ''

    if ($shippingCandidates.Count -eq 1) {
        $shippingCost = ('{0:0.00}' -f $shippingCandidates[0].Amount)
        $shippingRule = $shippingCandidates[0].Rule
    }
    elseif ($shippingCandidates.Count -gt 1) {
        $review.Add('MULTIPLE_SHIPPING_DOLLAR_CANDIDATES')
    }
    elseif ($amounts.Count -gt 0) {
        if ($noCharge) {
            $review.Add('DOLLAR_FOUND_WITH_NO_CUSTOMER_CHARGE_NOTE')
        }
        else {
            $review.Add('DOLLAR_FOUND_NOT_STRICT_SHIPPING_CONTEXT')
        }
    }

    if ($amounts.Count -gt 1) {
        $review.Add('MULTIPLE_DOLLAR_AMOUNTS_FOUND')
    }

    return [pscustomobject]@{
        ShippingCost = $shippingCost
        ShippingRule = $shippingRule
        NoCharge = if ($noCharge) { 'Y' } else { '' }
        DollarAmounts = ($amounts -join '; ')
        Review = (($review | Select-Object -Unique) -join '; ')
    }
}

function Estimate-ListedItemCount([string]$NormalizedText, [int]$StartIndex) {
    if ($StartIndex -lt 0 -or $StartIndex -ge $NormalizedText.Length) { return 0 }

    $tail = $NormalizedText.Substring($StartIndex)
    if ($tail.Length -gt 500) {
        $tail = $tail.Substring(0, 500)
    }

    $totalMatch = [regex]::Match($tail, '\bTOTAL\s+OF\s+(\d{1,3})\b')
    if ($totalMatch.Success) {
        return [int]$totalMatch.Groups[1].Value
    }

    $tail = [regex]::Replace($tail, '^\s*(?:OF\s+EACH|EACH\s+COLOR|EACH|OF|THE\s+FOLLOWING|FOR|:|\.|\*|-)+\s*', '')
    $tail = [regex]::Replace($tail, '^\s*[A-Z ]+-\s*\|', '')
    $tail = [regex]::Replace($tail, '\|\s*\d+\s*(?:CTNS?|BOXES|BXS|SHEETS?|LF|SF|S/F|SQFT|BF|BNDL|BUNDLES?).*$', '')
    $tail = [regex]::Replace($tail, '\|\s*(?:PAID|SHIP|SEND\s+UPS|NO\s+CHARGE|TR\s*#|LOT|PRICE|REFUND|CREDIT).*$', '')
    $tail = [regex]::Replace($tail, '\([^)]*\)', ' ')

    $pieces = @(
        [regex]::Split($tail, '\s*\|\s*|\s*,\s*|\s+\&\s+|\s+\bAND\b\s+|(?<!\d)\s+-+\s*(?=[A-Z])')
    )

    $filtered = New-Object System.Collections.Generic.List[string]
    foreach ($piece in $pieces) {
        $clean = ([regex]::Replace($piece, '\s+', ' ')).Trim(' ', ':', '-', '*', '.', ',')
        if (-not $clean) { continue }
        if ($clean.Length -lt 3) { continue }
        if ($clean -match '\b(PACKAGE|PKG|BOX|BXS|CTNS?|SHEETS?|BNDL|BUNDLES?|RP//|UPS|SHIP|GROUND|NO\s+CHARGE|PAID|VISA|AMEX|CASH|CHECK|TR#|LOT#|PRICE|SF|S/F|SQFT|LF|BF|CVV|EXP)\b') { continue }
        if ($clean -match '^\d+[\d\s./"-]*$') { continue }
        $filtered.Add($clean)
    }

    return $filtered.Count
}

function Get-ChipQtyInfo([string]$RawText, [string]$TypeValue) {
    $normalized = Normalize-Note $RawText
    $isChipOrder = ([string]$TypeValue).Trim() -match '(?i)CHIP' -or
        $normalized -match '\b(CHIP|CHIPS|SAMPLE\s+CHIP|SAMPLE\s+CHIPS)\b'

    if (-not $isChipOrder) {
        return [pscustomobject]@{
            IsChipOrder = ''
            Qty = ''
            Rule = ''
            Review = ''
        }
    }

    $review = New-Object System.Collections.Generic.List[string]

    $totalNearChip = [regex]::Match($normalized, '\bTOTAL\s+OF\s+(\d{1,3})\b.{0,60}\bCHIPS?\b|\bCHIPS?\b.{0,60}\bTOTAL\s+OF\s+(\d{1,3})\b')
    if ($totalNearChip.Success) {
        $value = if ($totalNearChip.Groups[1].Success) { $totalNearChip.Groups[1].Value } else { $totalNearChip.Groups[2].Value }
        return [pscustomobject]@{
            IsChipOrder = 'Y'
            Qty = [int]$value
            Rule = 'explicit_total_of_near_chip'
            Review = ''
        }
    }

    $patterns = @(
        [pscustomobject]@{
            Name = 'number_before_sample_chips'
            Pattern = '\b(?:PLEASE\s+SEND|PLESE\s+SEND|SEND(?:ING)?|SENBD|SNED|SEMD|SHIP|SEND\s+OUT)?\s*(\d{1,3})\s+(?:SAMPLE|SAMPLES)\s+CHIPS?\b'
        },
        [pscustomobject]@{
            Name = 'number_before_sample_boards'
            Pattern = '\b(?:PLEASE\s+SEND|SEND(?:ING)?|SEND\s+OUT)?\s*(\d{1,3})\s+(?:SAMPLE|SAMPLES)\s+BOARDS?\b'
        },
        [pscustomobject]@{
            Name = 'send_out_pcs_near_sample'
            Pattern = '\bSEND\s+OUT\s+(\d{1,3})\s*PCS\b'
        },
        [pscustomobject]@{
            Name = 'number_chips_each'
            Pattern = '\b(\d{1,3})\s+CHIPS?\s+EACH\b'
        }
    )

    $candidates = New-Object System.Collections.Generic.List[object]
    foreach ($entry in $patterns) {
        foreach ($match in [regex]::Matches($normalized, $entry.Pattern)) {
            $qty = [int]$match.Groups[1].Value
            if ($qty -lt 1 -or $qty -gt 300) { continue }

            $contextStart = [Math]::Max(0, $match.Index - 30)
            $contextLength = [Math]::Min($normalized.Length - $contextStart, 180)
            $context = $normalized.Substring($contextStart, $contextLength)

            if ($entry.Name -eq 'send_out_pcs_near_sample' -and $context -notmatch '\b(SAMPLE|CHIP|CHIPS)\b') {
                continue
            }

            $afterStart = [Math]::Min($normalized.Length, $match.Index + $match.Length)
            $afterLength = [Math]::Min($normalized.Length - $afterStart, 220)
            $after = if ($afterLength -gt 0) { $normalized.Substring($afterStart, $afterLength) } else { '' }

            $explicitEach = $context -match '\b(OF\s+EACH|EACH\s+COLOR|CHIPS?\s+EACH)\b' -or
                $after -match '^\s*(?:OF\s+EACH|EACH\s+COLOR)\b'
            $itemCount = if ($explicitEach) { Estimate-ListedItemCount $normalized $afterStart } else { 0 }
            $resolvedQty = $qty
            $rule = $entry.Name

            if ($explicitEach -and $itemCount -gt 1) {
                $resolvedQty = $qty * $itemCount
                $rule = "$($entry.Name)_times_$($itemCount)_listed_items"
                $review.Add('EACH_PATTERN_MULTIPLIED_BY_LISTED_ITEMS')
            }
            elseif ($explicitEach) {
                $review.Add('EACH_PATTERN_ITEM_COUNT_NOT_CONFIDENT')
            }

            $candidates.Add([pscustomobject]@{
                Qty = $resolvedQty
                BaseQty = $qty
                Index = $match.Index
                Rule = $rule
            })
        }
    }

    if ($candidates.Count -eq 0) {
        return [pscustomobject]@{
            IsChipOrder = 'Y'
            Qty = ''
            Rule = ''
            Review = 'NO_CHIP_QTY_FOUND'
        }
    }

    $unique = @($candidates | Sort-Object Index, Qty | Select-Object -Property Qty, Rule, Index -Unique)
    if ($unique.Count -gt 1) {
        $review.Add('MULTIPLE_CHIP_QTY_CANDIDATES')
    }

    $qtyTotal = ($unique | Measure-Object -Property Qty -Sum).Sum
    $rule = (($unique | ForEach-Object { $_.Rule }) | Select-Object -Unique) -join '; '

    return [pscustomobject]@{
        IsChipOrder = 'Y'
        Qty = [int]$qtyTotal
        Rule = $rule
        Review = (($review | Select-Object -Unique) -join '; ')
    }
}

$rows = Import-Csv -LiteralPath $InputPath
if (-not $rows) {
    throw "No rows found in $InputPath"
}

    $parsedRows = New-Object System.Collections.Generic.List[object]
$reviewRows = New-Object System.Collections.Generic.List[object]

foreach ($row in $rows) {
    $noteText = [string]$row.ORDER_NOTES
    $normalized = Normalize-Note $noteText
    $chipInfo = Get-ChipQtyInfo $noteText $row.TYPE
    $dollarInfo = Get-DollarInfo $noteText $normalized

    $manualQty = Convert-ToDecimalString $row.QTY
    $parsedQty = Convert-ToDecimalString $chipInfo.Qty
    $finalQty = ''
    $qtySource = ''
    $qtyReview = New-Object System.Collections.Generic.List[string]

    if ($chipInfo.Review) {
        foreach ($flag in ($chipInfo.Review -split ';\s*')) {
            if ($flag) { $qtyReview.Add($flag) }
        }
    }

    if ($manualQty) {
        $finalQty = $manualQty
        $qtySource = 'manual_existing_qty'

        $manualNumber = 0.0
        if ([double]::TryParse($manualQty, [ref]$manualNumber)) {
            if ([Math]::Abs($manualNumber - [Math]::Round($manualNumber)) -gt 0.0001) {
                $qtyReview.Add('MANUAL_QTY_IS_NOT_WHOLE_NUMBER')
            }
            if ($parsedQty) {
                $parsedNumber = 0.0
                if ([double]::TryParse($parsedQty, [ref]$parsedNumber) -and [Math]::Abs($manualNumber - $parsedNumber) -gt 0.0001) {
                    $qtyReview.Add('MANUAL_QTY_DIFFERS_FROM_PARSED_QTY')
                }
            }
        }
    }
    elseif ($parsedQty) {
        $finalQty = $parsedQty
        $qtySource = 'parsed_from_order_notes'
    }
    elseif ($chipInfo.IsChipOrder) {
        $qtySource = 'needs_review'
    }

    $dollarValues = @($dollarInfo.DollarAmounts -split ';\s*' | Where-Object { $_ })
    $uniqueDollarValues = @($dollarValues | Select-Object -Unique)
    $costCandidate = ''
    $costCandidateSource = ''
    $costCandidateReview = New-Object System.Collections.Generic.List[string]

    if ($dollarInfo.ShippingCost) {
        $costCandidate = $dollarInfo.ShippingCost
        $costCandidateSource = 'strict_shipping_freight_or_delivery_amount'
        if ($dollarInfo.NoCharge) {
            $costCandidateReview.Add('NO_CUSTOMER_CHARGE_LANGUAGE_PRESENT')
        }
    }
    elseif ($uniqueDollarValues.Count -eq 1) {
        $costCandidate = $uniqueDollarValues[0]
        $costCandidateSource = 'single_dollar_amount_in_order_notes'
        $costCandidateReview.Add('VERIFY_AMOUNT_IS_FREIGHT_OR_SAMPLE_COST')
        if ($dollarInfo.NoCharge) {
            $costCandidateReview.Add('NO_CUSTOMER_CHARGE_LANGUAGE_PRESENT')
        }
    }
    elseif ($uniqueDollarValues.Count -gt 1) {
        $costCandidateSource = 'multiple_dollar_amounts_needs_review'
        $costCandidateReview.Add('MULTIPLE_DOLLAR_AMOUNTS_REVIEW_REQUIRED')
        if ($dollarInfo.NoCharge) {
            $costCandidateReview.Add('NO_CUSTOMER_CHARGE_LANGUAGE_PRESENT')
        }
    }

    $ordered = [ordered]@{}
    foreach ($property in $row.PSObject.Properties) {
        $ordered[$property.Name] = $property.Value
    }
    $ordered['FINAL_CHIP_QTY'] = $finalQty
    $ordered['PARSED_CHIP_QTY'] = $parsedQty
    $ordered['CHIP_QTY_SOURCE'] = $qtySource
    $ordered['CHIP_QTY_RULE'] = $chipInfo.Rule
    $ordered['CHIP_QTY_REVIEW'] = (($qtyReview | Select-Object -Unique) -join '; ')
    $ordered['ORDER_NOTE_COST_CANDIDATE'] = $costCandidate
    $ordered['ORDER_NOTE_COST_CANDIDATE_SOURCE'] = $costCandidateSource
    $ordered['ORDER_NOTE_COST_REVIEW'] = (($costCandidateReview | Select-Object -Unique) -join '; ')
    $ordered['PARSED_SHIPPING_COST'] = $dollarInfo.ShippingCost
    $ordered['SHIPPING_COST_RULE'] = $dollarInfo.ShippingRule
    $ordered['SHIPPING_NO_CHARGE'] = $dollarInfo.NoCharge
    $ordered['CUSTOMER_SHIPPING_NO_CHARGE'] = $dollarInfo.NoCharge
    $ordered['DOLLAR_AMOUNTS_FOUND'] = $dollarInfo.DollarAmounts
    $ordered['DOLLAR_REVIEW'] = $dollarInfo.Review

    $outRow = [pscustomobject]$ordered
    $parsedRows.Add($outRow)

    if ($outRow.CHIP_QTY_REVIEW -or $outRow.DOLLAR_REVIEW -or $outRow.ORDER_NOTE_COST_REVIEW -or ($chipInfo.IsChipOrder -and -not $outRow.FINAL_CHIP_QTY)) {
        $reviewRows.Add($outRow)
    }
}

$parsedRows | Export-Csv -LiteralPath $OutputPath -NoTypeInformation -Encoding UTF8
$reviewRows | Export-Csv -LiteralPath $ReviewPath -NoTypeInformation -Encoding UTF8

$chipRows = @($parsedRows | Where-Object { $_.CHIP_QTY_SOURCE })
$manualRows = @($parsedRows | Where-Object { $_.CHIP_QTY_SOURCE -eq 'manual_existing_qty' })
$parsedFilledRows = @($parsedRows | Where-Object { $_.CHIP_QTY_SOURCE -eq 'parsed_from_order_notes' })
$unresolvedRows = @($parsedRows | Where-Object { $_.CHIP_QTY_SOURCE -eq 'needs_review' })
$shippingRows = @($parsedRows | Where-Object { $_.PARSED_SHIPPING_COST })
$costCandidateRows = @($parsedRows | Where-Object { $_.ORDER_NOTE_COST_CANDIDATE })
$noChargeRows = @($parsedRows | Where-Object { $_.SHIPPING_NO_CHARGE -eq 'Y' })

[pscustomobject]@{
    InputRows = $rows.Count
    ChipApplicableRows = $chipRows.Count
    ExistingManualQtyRows = $manualRows.Count
    BlankQtyRowsFilledByParser = $parsedFilledRows.Count
    ChipRowsStillNeedingReview = $unresolvedRows.Count
    RowsWithOrderNoteCostCandidate = $costCandidateRows.Count
    RowsWithParsedShippingCost = $shippingRows.Count
    RowsMarkedShippingNoCharge = $noChargeRows.Count
    ReviewRows = $reviewRows.Count
    OutputPath = (Resolve-Path -LiteralPath $OutputPath).Path
    ReviewPath = (Resolve-Path -LiteralPath $ReviewPath).Path
}
