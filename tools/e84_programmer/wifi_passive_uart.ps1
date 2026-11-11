param([Parameter(Mandatory)][string]$LogPath)
$ErrorActionPreference = 'Stop'
$ssid = $env:TCM_WIFI_CHECK_SSID
if ($ssid -notmatch '^[A-Za-z0-9_-]{1,32}$') { throw 'Set TCM_WIFI_CHECK_SSID before running this probe.' }
$serial = [System.IO.Ports.SerialPort]::new('COM3', 115200, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
$serial.DtrEnable = $false
$serial.RtsEnable = $false
$transcript = [Text.StringBuilder]::new()
try {
    $serial.Open()
    $serial.Write("wifi_passive $ssid`r")
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt 16000) {
        [void]$transcript.Append($serial.ReadExisting())
        if ($transcript.ToString() -match 'WIFI_(?:AU_READY ok=0|PASSIVE_DONE|PASSIVE_TIMEOUT|PASSIVE_START error=)[^\r\n]*\r?\n') { break }
        Start-Sleep -Milliseconds 35
    }
} finally {
    $serial.Dispose()
    [IO.File]::WriteAllText($LogPath, $transcript.ToString())
}
Get-Content -LiteralPath $LogPath
