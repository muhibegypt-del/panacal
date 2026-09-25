param(
    [Parameter(Mandatory=$true)][string]$ControlFile,
    [Parameter(Mandatory=$true)][string]$StatusFile,
    [string]$Screen = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DpiMode {
    [DllImport("user32.dll")]
    public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
'@
# Per-monitor DPI awareness so the window covers every physical pixel.
[DpiMode]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null
[System.Windows.Forms.Application]::EnableVisualStyles()

$screens = @([System.Windows.Forms.Screen]::AllScreens)
$inventory = @($screens | ForEach-Object { "$($_.DeviceName) primary=$($_.Primary) bounds=$($_.Bounds)" }) -join '; '
if ($Screen) {
    $target = @($screens | Where-Object { $_.DeviceName -eq $Screen })
    if ($target.Count -ne 1) { throw "Display '$Screen' was not found. Windows sees: $inventory" }
} else {
    $target = @($screens | Where-Object { -not $_.Primary })
    if ($target.Count -eq 0) {
        throw "No secondary display is active. Windows sees: $inventory. Set the LG TV to 'Extend these displays' in Windows Display Settings."
    }
    if ($target.Count -gt 1) {
        throw "More than one secondary display is active; set pattern.screen in settings.json. Windows sees: $inventory"
    }
}
$screen = $target[0]
$script:state = [ordered]@{ sequence = -1; r = 0; g = 0; b = 0; windowArea = 0.10 }

$form = New-Object System.Windows.Forms.Form
$form.Text = 'LG AutoCal Pattern'
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$form.Bounds = $screen.Bounds
$form.BackColor = [System.Drawing.Color]::Black
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.Cursor = [System.Windows.Forms.Cursors]::None

$form.Add_Paint({
    param($sender, $eventArgs)
    # The server sends exact 8-bit codes; nothing is scaled here.
    $clamp = { param($v) [int][Math]::Max(0, [Math]::Min(255, [int]$v)) }
    $r = & $clamp $script:state.r
    $g = & $clamp $script:state.g
    $b = & $clamp $script:state.b
    $area = [Math]::Max(0.01, [Math]::Min(1.0, [double]$script:state.windowArea))
    if ($area -ge 1.0) {
        $width = $sender.ClientSize.Width
        $height = $sender.ClientSize.Height
    } else {
        $side = [Math]::Sqrt($area)
        $width = [int]($sender.ClientSize.Width * $side)
        $height = [int]($sender.ClientSize.Height * $side)
    }
    $left = [int](($sender.ClientSize.Width - $width) / 2)
    $top = [int](($sender.ClientSize.Height - $height) / 2)
    $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb($r, $g, $b))
    try {
        $eventArgs.Graphics.FillRectangle($brush, $left, $top, $width, $height)
    } finally {
        $brush.Dispose()
    }
})

function Write-Status([int]$sequence) {
    $bounds = $form.Bounds
    $status = [ordered]@{
        ready = $true
        sequence = $sequence
        device = $screen.DeviceName
        width = $bounds.Width
        height = $bounds.Height
        r = $script:state.r
        g = $script:state.g
        b = $script:state.b
        window_area = $script:state.windowArea
    }
    $temp = "$StatusFile.tmp"
    $status | ConvertTo-Json | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $StatusFile -Force
}

$timer = New-Object System.Windows.Forms.Timer
# WinForms timers resolve to one ~15 ms system tick.
$timer.Interval = 15
$timer.Add_Tick({
    try {
        if (-not (Test-Path -LiteralPath $ControlFile)) { return }
        $next = Get-Content -Raw -LiteralPath $ControlFile | ConvertFrom-Json
        if ([int]$next.sequence -eq [int]$script:state.sequence) { return }
        if ([bool]$next.close) {
            $timer.Stop()
            $form.Close()
            return
        }
        $script:state.r = [int]$next.r
        $script:state.g = [int]$next.g
        $script:state.b = [int]$next.b
        $script:state.windowArea = [double]$next.window_area
        $form.Invalidate()
        $form.Update()
        Write-Status ([int]$next.sequence)
        # Mark the command done only once its acknowledgement is written; a
        # failed status write is retried on the next tick.
        $script:state.sequence = [int]$next.sequence
    } catch {
        # The command or status file may be mid-replacement; retry next tick.
    }
})

$form.Add_Shown({
    $form.Bounds = $screen.Bounds
    $form.Activate()
    Write-Status -1
    $timer.Start()
})

[System.Windows.Forms.Application]::Run($form)
