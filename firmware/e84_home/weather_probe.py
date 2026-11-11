"""Verify the IP location and QWeather data used by the E84 homepage."""

import argparse
from datetime import datetime, timezone
import gzip
import io
import json
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


IP_URL = "http://ip-api.com/json/?fields=status,countryCode,regionName,city,lat,lon,proxy&lang=zh-CN"
MAX_PAYLOAD = 128 * 1024


class WeatherError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def decode_json(body, encoding):
    if encoding == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
            body = compressed.read(MAX_PAYLOAD + 1)
    if len(body) > MAX_PAYLOAD:
        raise WeatherError("响应超过大小限制")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise WeatherError("响应不是 JSON 对象")
    return result


def request_json(url, service, headers=None):
    try:
        with build_opener(NoRedirect).open(Request(url, headers=headers or {}), timeout=10) as response:
            return decode_json(response.read(MAX_PAYLOAD + 1), response.headers.get("Content-Encoding"))
    except HTTPError as exc:
        raise WeatherError(f"{service} 返回 HTTP {exc.code}") from None
    except (URLError, OSError, EOFError, ValueError) as exc:
        raise WeatherError(f"{service} 请求或解码失败（{type(exc).__name__}）") from None


def coordinates(latitude, longitude):
    latitude, longitude = float(latitude), float(longitude)
    if not (math.isfinite(latitude) and math.isfinite(longitude) and
            -90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise WeatherError("坐标超出范围")
    return round(latitude, 2), round(longitude, 2)


def parse_location(data):
    if data.get("status") != "success":
        raise WeatherError("IP 定位服务未返回位置")
    lat, lon = coordinates(data["lat"], data["lon"])
    return {"city": data.get("city") or data.get("regionName") or "未知城市",
            "country_code": data.get("countryCode"), "latitude": lat, "longitude": lon,
            "proxy": data.get("proxy"), "source": "IP-API"}


def weather_paths(location):
    coords = f"{location['latitude']:.2f}/{location['longitude']:.2f}"
    return (f"/weather/v1/current/{coords}?lang=zh&localTime=true",
            f"/weather/v1/daily/{coords}?days=3&lang=zh&localTime=true")


def normalized_weather(location, current, forecast):
    days = forecast["days"]
    if len(days) != 3:
        raise WeatherError(f"和风天气返回 {len(days)} 天预报")
    sources = list(dict.fromkeys(
        source
        for payload in (current, forecast)
        for source in payload.get("metadata", {}).get("attributions", [])
    ))
    return {
        "location": location,
        "source": "和风天气",
        "attributions": sources,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current": {"valid_time": current.get("validTime"),
                    "condition": current["condition"]["text"],
                    "temperature_c": current["temperature"]["value"]},
        "daily": [{"date": day["forecastStartTime"][:10],
                   "condition": day["daytime"]["condition"]["text"],
                   "low_c": day["temperatureMin"]["value"],
                   "high_c": day["temperatureMax"]["value"]}
                  for day in days],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--locate-only", action="store_true")
    selection.add_argument("--location", nargs=3, metavar=("CITY", "LAT", "LON"))
    args = parser.parse_args()
    host = os.environ.get("QWEATHER_API_HOST", "")
    key = os.environ.get("QWEATHER_API_KEY", "")
    if not args.locate_only:
        if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.qweatherapi\.com", host):
            parser.error("请在 QWEATHER_API_HOST 中设置和风天气专属域名")
        if not key:
            parser.error("请设置 QWEATHER_API_KEY")
    try:
        if args.location:
            city, latitude, longitude = args.location
            lat, lon = coordinates(latitude, longitude)
            location = {"city": city, "latitude": lat, "longitude": lon, "source": "手动选择"}
        else:
            location = parse_location(request_json(IP_URL, "IP-API"))
        if args.locate_only:
            result = location
        else:
            headers = {"X-QW-Api-Key": key, "Accept-Encoding": "gzip"}
            current_path, daily_path = weather_paths(location)
            current = request_json(f"https://{host}{current_path}", "和风天气实况", headers)
            forecast = request_json(f"https://{host}{daily_path}", "和风天气预报", headers)
            result = normalized_weather(location, current, forecast)
    except (KeyError, TypeError, ValueError, WeatherError) as exc:
        parser.exit(1, f"{exc if isinstance(exc, WeatherError) else '接口返回的数据字段不完整'}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
