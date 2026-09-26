param(
    [Parameter(Mandatory=$true)][ValidateSet('prepare','restore')][string]$Action,
    [Parameter(Mandatory=$true)][string]$StateFile
)
# prepare: extend the desktop onto the TV if needed, find the LG output by
#          its EDID name, turn Windows HDR / advanced colour off on it, and
#          write what was changed to $StateFile (also printed as JSON).
# restore: undo exactly what prepare changed.
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'DisplayTool.cs'))

function Get-Outputs {
    @([DisplayTool]::Outputs() | ForEach-Object {
        [pscustomobject]@{
            gdi = $_.Gdi; name = $_.Name
            primary = ($_.X -eq 0 -and $_.Y -eq 0)
            x = $_.X; y = $_.Y; width = $_.Width; height = $_.Height
            hdr_supported = $_.HdrSupported; hdr_on = $_.HdrOn; bits = $_.Bits
            adapter_low = $_.AdapterLow; adapter_high = $_.AdapterHigh; target = $_.TargetId
        }
    })
}

function Select-Tv($outputs) {
    $named = @($outputs | Where-Object { $_.name -match 'LG' -and $_.name -match 'TV|OLED' })
    if ($named.Count -gt 0) { return ($named | Sort-Object primary | Select-Object -First 1) }
    $secondary = @($outputs | Where-Object { -not $_.primary })
    if ($secondary.Count -eq 1) { return $secondary[0] }
    if ($outputs.Count -eq 1) { return $outputs[0] }
    return $null
}

if ($Action -eq 'restore') {
    if (-not (Test-Path -LiteralPath $StateFile)) { exit 0 }
    $state = Get-Content -Raw -LiteralPath $StateFile | ConvertFrom-Json
    if ($state.hdr_changed) {
        [DisplayTool]::SetHdr([uint32]$state.adapter_low, [int]$state.adapter_high, [uint32]$state.target, $true) | Out-Null
    }
    if ($state.topology_changed -and [uint32]$state.topology_before -ne 0) {
        Start-Sleep -Milliseconds 500
        [DisplayTool]::SetTopology([uint32]$state.topology_before) | Out-Null
    }
    exit 0
}

$topology = [DisplayTool]::Topology()
$outputs = Get-Outputs
$distinct = @($outputs | Select-Object -ExpandProperty gdi -Unique)
$topologyChanged = $false
# Duplicate, PC-only or second-screen-only: switch to Extend so the TV gets
# its own desktop for the patches (restored afterwards). With a single
# display connected this fails harmlessly and the TV is used as it is.
if ($topology -ne [DisplayTool]::TOPOLOGY_EXTEND -and $distinct.Count -lt 2) {
    if ([DisplayTool]::SetTopology([DisplayTool]::TOPOLOGY_EXTEND) -eq 0) {
        $topologyChanged = $true
        Start-Sleep -Seconds 4
        $outputs = Get-Outputs
    }
}
$hdrChanged = $false
$tv = $null
try {
    $tv = Select-Tv $outputs
    if ($null -eq $tv) {
        $names = ($outputs | ForEach-Object { "$($_.gdi) '$($_.name)'" }) -join ', '
        throw "Could not tell which display is the LG TV. Windows sees: $names"
    }
    if ($tv.hdr_on) {
        if ([DisplayTool]::SetHdr($tv.adapter_low, $tv.adapter_high, $tv.target, $false) -eq 0) {
            $hdrChanged = $true
            Start-Sleep -Seconds 3
            $refreshed = @(Get-Outputs | Where-Object { $_.target -eq $tv.target -and $_.adapter_low -eq $tv.adapter_low })
            if ($refreshed.Count -gt 0) { $tv = $refreshed[0] }
        }
    }
    # Night light tints the whole desktop warm through the GPU; a calibration
    # measured through it would bake the tint into the TV. Windows keeps its on
    # state in this CloudStore blob (byte 18 is 0x15 while it is on).
    $nightLight = $false
    try {
        $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current\default$windows.data.bluelightreduction.bluelightreductionstate\windows.data.bluelightreduction.bluelightreductionstate'
        $data = (Get-ItemProperty -LiteralPath $key -Name Data -ErrorAction Stop).Data
        if ($data.Length -gt 18 -and $data[18] -eq 0x15) { $nightLight = $true }
    } catch {
        # No such key: Night light has never been switched on on this PC.
    }
    $state = [ordered]@{
        device = $tv.gdi; name = $tv.name; primary = $tv.primary
        x = $tv.x; y = $tv.y; width = $tv.width; height = $tv.height; bits = $tv.bits; hdr_on = $tv.hdr_on
        adapter_low = $tv.adapter_low; adapter_high = $tv.adapter_high; target = $tv.target
        topology_before = $topology; topology_changed = $topologyChanged; hdr_changed = $hdrChanged
        night_light = $nightLight
        outputs = @($outputs | ForEach-Object { "$($_.gdi) '$($_.name)' primary=$($_.primary)" })
    }
    $json = $state | ConvertTo-Json -Depth 3
    Set-Content -LiteralPath $StateFile -Value $json -Encoding UTF8
    Write-Output $json
} catch {
    # Leave Windows as it was found before reporting the failure.
    if ($hdrChanged) { [DisplayTool]::SetHdr($tv.adapter_low, $tv.adapter_high, $tv.target, $true) | Out-Null }
    if ($topologyChanged -and $topology -ne 0) { [DisplayTool]::SetTopology($topology) | Out-Null }
    throw
}
