function Decode-BoardResponse([string]$Exchange) {
    if ($Exchange -notmatch 'WEATHER_WIRE_END bytes=(\d+)') { throw 'Incomplete board HTTP response.' }
    $expected = [int]$Matches[1]
    $hex = [Text.RegularExpressions.Regex]::Matches($Exchange, '(?m)^W:([0-9a-f]+)\r?$') |
        ForEach-Object { $_.Groups[1].Value }
    $joined = $hex -join ''
    if ($joined.Length -ne ($expected * 2)) { throw 'UART payload length mismatch.' }
    $bytes = [byte[]]::new($expected)
    for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($joined.Substring(2 * $i, 2), 16) }
    $ascii = [Text.Encoding]::ASCII.GetString($bytes)
    if ($ascii -notmatch '^HTTP/1\.[01] (\d{3})\b') { throw 'Missing HTTP status.' }
    $status = [int]$Matches[1]
    if ($status -ne 200) { throw "Board HTTP status $status." }
    $boundary = $ascii.IndexOf("`r`n`r`n", [StringComparison]::Ordinal)
    if ($boundary -lt 0) { throw 'Missing HTTP header boundary.' }
    $header = $ascii.Substring(0, $boundary)
    $offset = $boundary + 4
    $body = [IO.MemoryStream]::new()
    try {
        if ($header -match '(?im)^Transfer-Encoding:\s*chunked\s*$') {
            while ($offset -lt $bytes.Length) {
                $end = $ascii.IndexOf("`r`n", $offset, [StringComparison]::Ordinal)
                if ($end -lt 0) { throw 'Incomplete chunk size.' }
                $size = [Convert]::ToInt32(($ascii.Substring($offset, $end - $offset) -split ';')[0], 16)
                $offset = $end + 2
                if ($size -eq 0) { break }
                if ($size -lt 0 -or ($offset + $size + 2) -gt $bytes.Length) { throw 'Incomplete chunk.' }
                $body.Write($bytes, $offset, $size)
                $offset += $size + 2
            }
        } else {
            $body.Write($bytes, $offset, $bytes.Length - $offset)
            if ($header -match '(?im)^Content-Length:\s*(\d+)\s*$' -and $body.Length -ne [int]$Matches[1]) {
                throw 'HTTP body length mismatch.'
            }
        }
        $body.Position = 0
        if ($header -match '(?im)^Content-Encoding:\s*gzip\s*$') {
            $gzip = [IO.Compression.GZipStream]::new($body, [IO.Compression.CompressionMode]::Decompress, $true)
            $decoded = [IO.MemoryStream]::new()
            try {
                $buffer = [byte[]]::new(4096)
                while (($n = $gzip.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $decoded.Write($buffer, 0, $n)
                    if ($decoded.Length -gt 131072) { throw 'Expanded payload too large.' }
                }
                return ([Text.Encoding]::UTF8.GetString($decoded.ToArray()) | ConvertFrom-Json)
            } finally {
                $gzip.Dispose()
                $decoded.Dispose()
            }
        }
        return ([Text.Encoding]::UTF8.GetString($body.ToArray()) | ConvertFrom-Json)
    } finally {
        $body.Dispose()
    }
}

function Invoke-BoardWeather([string]$Path) {
    $source = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = [byte[]]::new(48)
    try { $source.GetBytes($bytes) } finally { $source.Dispose() }
    $seed = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    $epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $ack = Invoke-BoardCommand "weather_seed $epoch $seed" 2000
    if ($ack -notmatch 'WEATHER_SEED_READY') { throw 'Board TLS clock/entropy setup failed.' }
    $response = Invoke-BoardCommand "weather_wire $Path $weatherKey" 18000
    if ($response -notmatch 'WEATHER_TLS_OK version=TLSv1\.2') { throw 'Board TLS handshake failed.' }
    return (Decode-BoardResponse $response)
}

function Invoke-WeatherExchange {
    if ($weatherKey -notmatch '^[a-fA-F0-9]{32}$') { throw 'Invalid weather key format.' }
    $report = [ordered]@{}
    try {
        $response = Invoke-BoardCommand 'weather_wire ip' 15000
        $ip = Decode-BoardResponse $response
        if ($ip.status -ne 'success') { throw 'IP-API lookup did not return success.' }
        $latitude = [double]$ip.lat
        $longitude = [double]$ip.lon
        if ([double]::IsNaN($latitude) -or [double]::IsNaN($longitude) -or
            [Math]::Abs($latitude) -gt 90 -or [Math]::Abs($longitude) -gt 180) { throw 'Invalid location coordinates.' }
        $report.ip_location = [ordered]@{ city = $ip.city; country_code = $ip.countryCode; proxy = $ip.proxy }
    } catch {
        $report.ip_error = $_.Exception.Message
    }
    if ($report.ip_location -and $report.ip_location.country_code -eq 'CN') {
        $report.weather_city = $ip.city
        $report.weather_location_source = 'ip-api'
    } else {
        $latitude = 39.90
        $longitude = 116.41
        $report.weather_city = 'Beijing'
        $report.weather_location_source = 'sample'
    }
    $culture = [Globalization.CultureInfo]::InvariantCulture
    $lat = $latitude.ToString('F2', $culture)
    $lon = $longitude.ToString('F2', $culture)
    try {
        $current = Invoke-BoardWeather "/weather/v1/current/$lat/${lon}?lang=zh&localTime=true"
        $report.current = [ordered]@{
            condition = $current.condition.text
            temperature_c = $current.temperature.value
            valid_time = $current.validTime
        }
        $forecast = Invoke-BoardWeather "/weather/v1/daily/$lat/${lon}?days=3&lang=zh&localTime=true"
        if ($forecast.days.Count -ne 3) { throw 'Unexpected forecast length.' }
        $report.daily = @($forecast.days | ForEach-Object {
            [ordered]@{ date = $_.forecastStartTime.Substring(0, 10); condition = $_.daytime.condition.text
                low_c = $_.temperatureMin.value; high_c = $_.temperatureMax.value }
        })
        $report.attributions = @($current.metadata.attributions + $forecast.metadata.attributions | Select-Object -Unique)
    } catch {
        $report.weather_error = $_.Exception.Message
    }
    $json = $report | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText((Join-Path (Split-Path -Parent $LogPath) 'board_weather.json'), $json)
}
