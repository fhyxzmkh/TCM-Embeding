param([string]$LogPath, [string]$ReadyPath, [string]$StopPath)
$ErrorActionPreference = 'Stop'
$serial = [System.IO.Ports.SerialPort]::new('COM3',115200,[System.IO.Ports.Parity]::None,8,[System.IO.Ports.StopBits]::One)
$serial.Handshake = [System.IO.Ports.Handshake]::None
$serial.DtrEnable = $false
$serial.RtsEnable = $false
$serial.ReadBufferSize = 32768
$log = [System.IO.FileStream]::new($LogPath,[System.IO.FileMode]::Create,[System.IO.FileAccess]::Write,[System.IO.FileShare]::ReadWrite)
$buffer = [byte[]]::new(8192)
try {
    $serial.Open()
    [System.IO.File]::WriteAllText($ReadyPath, 'COM3 open 115200 8N1')
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt 240 -and -not (Test-Path -LiteralPath $StopPath)) {
        if ($serial.BytesToRead -gt 0) {
            $count = $serial.Read($buffer,0,[Math]::Min($buffer.Length,$serial.BytesToRead))
            $log.Write($buffer,0,$count)
            $log.Flush()
        } else { Start-Sleep -Milliseconds 30 }
    }
} finally {
    $log.Dispose()
    $serial.Dispose()
}
