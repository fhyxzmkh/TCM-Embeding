param([switch]$SyncClock, [string]$LogPath)
$ErrorActionPreference = 'Stop'
$serial = [System.IO.Ports.SerialPort]::new('COM3', 115200, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
$serial.DtrEnable = $false
$serial.RtsEnable = $false

function Receive-Home([int]$DurationMs) {
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt $DurationMs) {
        $part = $serial.ReadExisting()
        if ($part.Length -gt 0) {
            [System.IO.File]::AppendAllText($LogPath, $part)
        }
        Start-Sleep -Milliseconds 35
    }
}

try {
    $serial.Open()
    $serial.Write("`r")
    Receive-Home 300
    if ($SyncClock) {
        $now = [DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(8))
        $stamp = $now.ToString('yyyy MM dd HH mm ss', [System.Globalization.CultureInfo]::InvariantCulture)
        $serial.Write("home_set_time $stamp`r")
        Receive-Home 800
    }
    $serial.Write("home_status`r")
    Receive-Home 1100
    $serial.Write("home_status`r")
    Receive-Home 1300
} finally {
    $serial.Dispose()
}
Get-Content -LiteralPath $LogPath
