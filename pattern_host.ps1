param(
    [Parameter(Mandatory=$true)][string]$ControlFile,
    [Parameter(Mandatory=$true)][string]$StatusFile
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
[DpiMode]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null
[System.Windows.Forms.Application]::EnableVisualStyles()

$screens = @([System.Windows.Forms.Screen]::AllScreens)
$target = @($screens | Where-Object { -not $_.Primary })
if ($target.Count -ne 1) {
    $inventory = @($screens | ForEach-Object {
        "$($_.DeviceName) primary=$($_.Primary) bounds=$($_.Bounds)"
    }) -join '; '
    if ($target.Count -eq 0) {
        throw "No secondary display is active. Windows sees: $inventory. Enable the Panasonic in Windows Display Settings using Extend these displays, then rerun AutoCal."
    }
    throw "Expected exactly one secondary display for the Panasonic pattern; found $($target.Count). Windows sees: $inventory."
}
$screen = $target[0]
$script:state = [ordered]@{ sequence = -1; stimulus = 0; code = 0; range = 'full'; close = $false; windowArea = 0.10 }

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Panasonic AutoCal Pattern'
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$form.Bounds = $screen.Bounds
$form.BackColor = [System.Drawing.Color]::Black
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.Cursor = [System.Windows.Forms.Cursors]::None

$form.Add_Paint({
    param($sender, $eventArgs)
    # The controller sends the exact 8-bit code; nothing is rounded here.
    $code = [int][Math]::Max(0, [Math]::Min(255, [int]$script:state.code))
    $side = [Math]::Sqrt([Math]::Max(0.01, [Math]::Min(1.0, [double]$script:state.windowArea)))
    $width = [int][Math]::Round($sender.ClientSize.Width * $side)
    $height = [int][Math]::Round($sender.ClientSize.Height * $side)
    $left = [int](($sender.ClientSize.Width - $width) / 2)
    $top = [int](($sender.ClientSize.Height - $height) / 2)
    $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb($code, $code, $code))
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
        primary = $screen.Primary
        left = $bounds.Left
        top = $bounds.Top
        width = $bounds.Width
        height = $bounds.Height
        stimulus = $script:state.stimulus
        code = $script:state.code
        range = $script:state.range
        window_area = $script:state.windowArea
    }
    $temp = "$StatusFile.tmp"
    $status | ConvertTo-Json | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $StatusFile -Force
}

$timer = New-Object System.Windows.Forms.Timer
# WinForms timers resolve to roughly one 15 ms system tick.
$timer.Interval = 15
$timer.Add_Tick({
    try {
        if (-not (Test-Path -LiteralPath $ControlFile)) { return }
        $next = Get-Content -Raw -LiteralPath $ControlFile | ConvertFrom-Json
        if ([int]$next.sequence -eq [int]$script:state.sequence) { return }
        $script:state.stimulus = [int]$next.stimulus
        $script:state.code = [int]$next.code
        $script:state.range = [string]$next.range
        if ($null -ne $next.window_area) {
            $script:state.windowArea = [double]$next.window_area
        }
        $script:state.close = [bool]$next.close
        if ($script:state.close) {
            $timer.Stop()
            $form.Close()
            return
        }
        $form.Invalidate()
        $form.Update()
        Write-Status ([int]$next.sequence)
        # Only mark the command done once its acknowledgement is written. If
        # the controller was reading the status file and the move failed, the
        # next tick repaints the same patch and retries the acknowledgement.
        $script:state.sequence = [int]$next.sequence
    } catch {
        # A command or status file may be mid-replacement; retry on the next tick.
    }
})

$form.Add_Shown({
    $form.Bounds = $screen.Bounds
    $form.Activate()
    Write-Status -1
    $timer.Start()
})

[System.Windows.Forms.Application]::Run($form)
