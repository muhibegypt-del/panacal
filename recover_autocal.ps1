param([switch]$InspectOnly)
$ErrorActionPreference = 'Stop'
$recoveryRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$config = Get-Content -Raw -LiteralPath (Join-Path $recoveryRoot 'config.json') | ConvertFrom-Json
$patternPath = Join-Path $recoveryRoot 'pattern_host.ps1'
$autoPath = Join-Path $recoveryRoot 'autocal.py'
$patternFileArg = '(?i)(?:^|\s)-File\s+(?:"' + [regex]::Escape($patternPath) + '"|' + [regex]::Escape($patternPath) + ')(?=\s|$)'
$absoluteAutoArg = '(?i)(?:^|\s)(?:"' + [regex]::Escape($autoPath) + '"|' + [regex]::Escape($autoPath) + ')(?=\s|$)'
$relativeAutoArg = '(?i)(?:^|\s)"?autocal\.py"?(?=\s|$)'
$all = @(Get-CimInstance Win32_Process)
$ownedHosts = @($all | Where-Object {
    $_.ProcessId -ne $PID -and $_.Name -match '^(powershell|pwsh)\.exe$' -and
    $_.CommandLine -match $patternFileArg
})
$ownerIds = @($ownedHosts | ForEach-Object { [int]$_.ParentProcessId })
$controllers = @($all | Where-Object {
    $_.Name -match '^python(w)?(\d+(\.\d+)?)?\.exe$' -and
    (($_.CommandLine -match $absoluteAutoArg) -or
     ($ownerIds -contains [int]$_.ProcessId -and $_.CommandLine -match $relativeAutoArg))
})
$ownerIds += @($controllers | ForEach-Object { [int]$_.ProcessId })
$meterExe = [string]$config.meter.default_executable
$correction = [string]$config.meter.correction_file
$ownedMeters = @($all | Where-Object {
    if ($_.Name -ine 'spotread.exe') { return $false }
    if ($ownerIds -contains [int]$_.ParentProcessId) { return $true }
    # Also recover a configured orphan after its pattern host has already gone.
    $meterParentId = [int]$_.ParentProcessId
    $parentExists = @($all | Where-Object { [int]$_.ProcessId -eq $meterParentId }).Count -gt 0
    return (-not $parentExists -and $_.ExecutablePath -ieq $meterExe -and
            $_.CommandLine -like ('*' + $correction + '*') -and
            $_.CommandLine -match '(?i)(?:^|\s)R:60(?=\s|$)')
})
$targets = @($controllers) + @($ownedMeters) + @($ownedHosts)
$targets = @($targets | Sort-Object ProcessId -Unique)
Write-Host ''
Write-Host 'PANASONIC AUTOCAL V4 - RECOVERY'
if ($targets.Count -eq 0) {
    Write-Host 'No leftover V4 controller, patch or meter processes found.'
} else {
    $targets | Select-Object Name, ProcessId, ParentProcessId | Format-Table -AutoSize
}
if ($InspectOnly) {
    Write-Host 'Inspection only: nothing stopped.'
    exit 0
}

$recoveryLog = Join-Path $recoveryRoot ('sessions\recovery_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.json')
$targets | Select-Object Name,ProcessId,ParentProcessId,CreationDate,CommandLine |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $recoveryLog -Encoding UTF8

function Stop-OwnedProcess($record) {
    $live = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$record.ProcessId)
    if (-not $live) { return }
    # Verify identity again so a recycled PID cannot target a different process.
    if ($live.CreationDate -ne $record.CreationDate -or $live.CommandLine -cne $record.CommandLine) {
        throw ('Process identity changed for PID ' + $record.ProcessId + '; rerun recovery.')
    }
    Write-Host ('Closing ' + $live.Name + ' PID ' + $live.ProcessId)
    Stop-Process -Id ([int]$live.ProcessId) -Force -ErrorAction Stop
    $remaining = Get-Process -Id ([int]$live.ProcessId) -ErrorAction SilentlyContinue
    if ($remaining -and -not $remaining.WaitForExit(5000)) {
        throw ('Process did not exit: ' + $live.ProcessId)
    }
}

# Stop the controller first so it cannot launch another meter or patch host.
foreach ($record in $controllers) { Stop-OwnedProcess $record }
foreach ($record in $ownedMeters) { Stop-OwnedProcess $record }
foreach ($record in $ownedHosts) { Stop-OwnedProcess $record }

Write-Host ''
Write-Host 'Cleanup complete. The patch is closed and the meter is released.'
Write-Host 'Existing calibration logs and snapshots are saved in sessions.'
Write-Host ''
Write-Host 'On the TV: exit isfccc Network, then reopen it at Waiting for Connection.'
Write-Host 'If the TV still reports a busy connection, put it in standby, turn it back on, and re-arm.'
Write-Host 'The next V4 run will calibrate from your current TV settings.'
