param([Parameter(Mandatory)][string]$LogPath)
$ErrorActionPreference = 'Stop'
$ssid = $env:TCM_WIFI_CHECK_SSID
$key = $env:TCM_WIFI_CHECK_KEY
$weatherKey = $env:TCM_WIFI_WEATHER_KEY
if ($ssid -notmatch '^[A-Za-z0-9_-]{1,32}$' -or $key -notmatch '^[A-Za-z0-9_-]{8,32}$') {
    throw 'Missing or unsupported temporary hotspot credentials.'
}
$serial = [System.IO.Ports.SerialPort]::new('COM3', 115200, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
$serial.DtrEnable = $false
$serial.RtsEnable = $false

function Invoke-BoardCommand([string]$Command, [int]$WaitMs) {
    $serial.Write($Command + "`r")
    $result = [Text.StringBuilder]::new()
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt $WaitMs) {
        [void]$result.Append($serial.ReadExisting())
        if ($result.ToString() -match 'msh (?:/[^>\r\n]*)?>\s*$') { break }
        Start-Sleep -Milliseconds 35
    }
    $record = $result.ToString().Replace($key, '[redacted]')
    if ($weatherKey) { $record = $record.Replace($weatherKey, '[redacted]') }
    $logText = $record -replace '(?m)^W:[0-9a-f]+\r?\n', ''
    [IO.File]::AppendAllText($LogPath, $logText + [Environment]::NewLine)
    return $record
}

try {
    $serial.Open()
    $serial.DiscardInBuffer()
    $scan = Invoke-BoardCommand "wifi_passive $ssid" 16000
    if ($scan -match 'WIFI_PASSIVE_DONE ok=1.* seen=1') {
        $joined = Invoke-BoardCommand "wifi_join_limited $key" 18000
        if ($joined -match 'WIFI_ASSOCIATED channel=\d+') {
            for ($attempt = 0; $attempt -lt 25; $attempt++) {
                $config = Invoke-BoardCommand 'ifconfig' 1800
                if ($config -match 'DHCP_ENABLE' -and
                    $config -match 'ip address:\s+(?!0\.0\.0\.0|127\.)(?:\d+\.){3}\d+') { break }
                Start-Sleep -Milliseconds 800
            }
            if ($config -match 'DHCP_ENABLE' -and
                $config -match 'ip address:\s+(?!0\.0\.0\.0|127\.)(?:\d+\.){3}\d+') {
                if ($weatherKey) {
                    . "$PSScriptRoot/wifi_weather_exchange.ps1"
                    Invoke-WeatherExchange
                } else {
                    $null = Invoke-BoardCommand 'wifi_linkcheck restapi.amap.com' 15000
                }
            }
            $null = Invoke-BoardCommand 'wifi_end_test' 5000
        }
    }
} finally {
    $serial.Dispose()
}
Get-Content -LiteralPath $LogPath
