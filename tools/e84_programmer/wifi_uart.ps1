param([Parameter(Mandatory)][string]$LogPath)
$ErrorActionPreference = 'Stop'
$ssid = $env:TCM_WIFI_CHECK_SSID
$key = $env:TCM_WIFI_CHECK_KEY
if ($ssid -notmatch '^[A-Za-z0-9_-]{1,32}$' -or $key -notmatch '^[A-Za-z0-9_-]{8,32}$') {
    throw 'Missing or unsupported temporary hotspot credentials.'
}
$serial = [System.IO.Ports.SerialPort]::new('COM3', 115200, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
$serial.DtrEnable = $false
$serial.RtsEnable = $false
$transcript = [Text.StringBuilder]::new()

function Invoke-BoardCommand([string]$Command, [int]$WaitMs) {
    $serial.Write($Command + "`r")
    $result = [Text.StringBuilder]::new()
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt $WaitMs) {
        [void]$result.Append($serial.ReadExisting())
        if ($result.ToString() -match 'msh (?:/[^>\r\n]*)?>\s*$') { break }
        Start-Sleep -Milliseconds 35
    }
    return $result.ToString()
}

try {
    $serial.Open()
    $null = Invoke-BoardCommand '' 3000
    [void]$transcript.AppendLine((Invoke-BoardCommand 'list_thread' 2500))
    [void]$transcript.AppendLine('--- filtered scan ---')
    [void]$transcript.AppendLine((Invoke-BoardCommand "wifi scan $ssid" 12000))
    [void]$transcript.AppendLine('--- hotspot association ---')
    [void]$transcript.AppendLine((Invoke-BoardCommand "wifi join $ssid $key" 14000))
    [void]$transcript.AppendLine((Invoke-BoardCommand 'wifi status' 2500))

    [void]$transcript.AppendLine('--- DHCP ---')
    $config = ''
    for ($attempt = 0; $attempt -lt 8; $attempt++) {
        $config = Invoke-BoardCommand 'ifconfig' 2000
        if ($config -match 'ip address:\s+(?!0\.0\.0\.0|127\.)(?:\d+\.){3}\d+') { break }
        Start-Sleep -Milliseconds 900
    }
    [void]$transcript.AppendLine($config)
    if ($config -match 'ip address:\s+(?!0\.0\.0\.0|127\.)(?:\d+\.){3}\d+') {
        [void]$transcript.AppendLine('--- DNS and outbound TCP ---')
        [void]$transcript.AppendLine((Invoke-BoardCommand 'wifi_linkcheck restapi.amap.com' 18000))
    }
    [void]$transcript.AppendLine('--- end association ---')
    [void]$transcript.AppendLine((Invoke-BoardCommand 'wifi disc' 3000))
} finally {
    $serial.Dispose()
    [IO.File]::WriteAllText($LogPath, $transcript.ToString().Replace($key, '[redacted]'))
}
Get-Content -LiteralPath $LogPath
