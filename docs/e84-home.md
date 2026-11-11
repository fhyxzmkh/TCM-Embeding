# E84 首页「四时小记」

首页使用 Edgi-Talk 的 480×800 屏幕、板载 RTC 和 AHT20。屏幕显示日期、星期、时间、当前节气和月历，标出当天日期。天气版的「今日推荐」优先显示和风生活指数建议；未取得有效建议时，使用随日期和季节变化的本地提示。温湿度约每 5 秒采样一次，I²C 或校验失败时显示占位符。

- 时区采用北京时间（UTC+8）。节气表覆盖 2025–2035 年，节气日当天显示「今日节气」。表外仍显示日期和月历。日期数据来自 [香港天文台公历与农历日期对照表](https://www.hko.gov.hk/tc/gts/time/conversion.htm)。
- 环境提示阈值为相对湿度低于 30% 或达到 60%、温度低于 18 °C 或达到 28 °C。湿度提示优先，温度采用项目设定的阈值。湿度参考 [美国环保署室内湿度说明](https://www.epa.gov/mold/mold-course-chapter-2)。
- AHT20 使用 P9.3（SCL）和 P9.2（SDA）。触摸屏继续使用 LVGL 例程的 P13.1 和 P13.2。传感器数据经过状态位和 CRC 校验。引脚和协议参考 [RT-Thread Edgi-Talk SDK](https://github.com/RT-Thread-Studio/sdk-bsp-psoc_e84-edgi-talk/tree/b135be8a28127e276a04b70d27950c3ba07b917a) 和 [奥松 AHT20 说明书](https://www.aosong.com/userfiles/files/media/AHT20%20%E8%8B%B1%E6%96%87%E7%89%88%E8%AF%B4%E6%98%8E%E4%B9%A6%20A0%2020201222.pdf)。

## 构建

沿用 SDK 提交 `b135be8a28127e276a04b70d27950c3ba07b917a`、ARM GNU 13.3.1 和已有的 RT-Thread Studio SCons。构建脚本从厂商 M55 LVGL 工程生成独立的 `TCM_Home` 工程。

```powershell
python firmware/e84_home/prepare_home.py --sdk data/e84_bsp_src --assets data/e84_home_assets
python firmware/e84_static_probe/build_with_scons.py --project data/e84_bsp_src/projects/TCM_Home --gcc-bin <ARM-GCC-bin> --scons-package <SCons包所在目录>
```

`data/e84_home_assets` 中放置香港天文台的 `T2025c.txt` 至 `T2035c.txt` 和 [Noto Sans CJK SC Regular](https://github.com/notofonts/noto-cjk/blob/Sans2.004/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf)。准备脚本用 Pillow 将首页实际使用的文字生成 LVGL 字形。字体许可证见 [OFL.txt](../firmware/e84_home/OFL.txt)。

## 校时与诊断

天气版先连接热点。RTC 丢失时间时，设备联网后通过 NTP 自动校准北京时间，再请求天气。校时服务暂不可用时，设备保持联网并自动重试。校时前首页显示「日期待校时」。离线版或手动校时可通过 KitProg3 串口连接 MSH，115200 8N1。把下例参数替换为当前北京时间。

```text
home_set_time 2026 11 05 15 30 00
home_status
```

`home_status` 输出 RTC、节气索引和传感器状态。`env=1` 表示有效数据，`temp10` 和 `rh10` 分别是以 0.1 °C 和 0.1% 为单位的读数。首帧完成后串口输出 `TCM_HOME_FIRST_FRAME`。

## 上板记录

- 已烧录 M55 首页固件，Flash 回读校验 742,024 字节。烧录日志、原 M55 受影响扇区备份和首页固件保存在 `runs/e84_home`。
- 2026-11-05 串口确认 RTC 按北京时间走时，节气为霜降，AHT20 实测 30.5 °C、47.6% RH。帧缓冲截图显示 11 月月历、高亮的 5 日及偏暖提示。
- RTC 在软件复位后保留了时间。完全断电后实测 RTC 回到 2000 年，天气版会联网恢复时间。

## 定位与天气接口验证

`weather_probe.py` 使用 Python 标准库。IP-API 以当前联网出口估计城市，和风天气按坐标返回实况和三天预报。网络出口与设备所在地不一致时，可用 `--location` 指定城市和坐标。配置从环境变量 `QWEATHER_API_HOST` 和 `QWEATHER_API_KEY` 读取，`--locate-only` 只请求 IP-API。接口文档见 [IP-API](https://ip-api.com/docs/api:json)、[和风天气实况](https://dev.qweather.com/docs/api/weather/weather-current/)和[逐日预报](https://dev.qweather.com/docs/api/weather/weather-daily-forecast/)。

```powershell
python firmware/e84_home/weather_probe.py --locate-only
python firmware/e84_home/weather_probe.py --location 北京 39.90 116.41
```

2026-11-05 使用独立 M55 测试固件，经手机热点验证了和风天气的 TLS 1.2、域名与证书校验。实况与三天预报均返回 HTTP 200，电脑从串口接收并解压 gzip 响应。该次热点连接到 IP-API 的 HTTP 端口失败，天气测试使用北京示例坐标。测试固件使用电脑经串口提供的校验时间和一次性随机种子，正式首页接入时需要板端独立提供这两项。原首页已刷回并通过逐字节回读。

## 自动定位、手动选城和首页天气

点击首页的「室外天气」卡片，打开天气详情。下拉列表提供「自动定位」和 33 个手选城市。每次启动默认自动定位，以北京作为初始备用城市。手动选城后，设备按所选城市更新天气，并将它作为自动定位失败时的备用城市。重新选择「自动定位」即可恢复按网络出口定位。

设备先访问 [IP-API 免费接口](https://ip-api.com/docs/api:json)，请求失败时通过 HTTPS 访问 [IP.SB](https://ip.sb/api/)。两者都按联网出口估算位置。IP-API 标记为代理、托管网络，或返回大陆以外的位置时，设备使用手选城市，并在详情说明原因。IP-API 的 HTTP 请求不携带天气凭据，板端遵守其限流等待时间。

天气成功后每 20 分钟更新，失败后约 30 秒重试。详情页的「刷新天气」在自动模式下也会重新定位。同地点更新失败时保留上次数据，切换地点时清空上一地点的数据。首页和详情采用暖米色与低饱和墨绿，LCD 背光默认 65%。

天气版使用原厂 Wi-Fi 工程的无线资源，以及 XiaoZhi 工程的 mbedTLS 与 cJSON 包。板上使用 RTC 验证服务器证书、使用芯片 TRNG 初始化 TLS 随机数，并在本地解压 gzip 响应。构建需把下列资源放入 `data/e84_home_assets`。

- `isrgrootx1.pem`，和风天气证书链所需的 [ISRG Root X1](https://letsencrypt.org/certs/isrgrootx1.pem)。
- `gtsr4.pem`，IP.SB 证书链所需的 [GTS Root R4](https://pki.goog/repo/certs/gtsr4.pem)。
- `China-City-List.csv`，用于生成中文地名字形和中英文名称对应表的[和风天气城市列表](https://raw.githubusercontent.com/qwd/LocationList/bf62f4934f9b2e8260d8a49ee0cf8eb8a948eda3/China-City-List-latest.csv)。

```powershell
python firmware/e84_home/prepare_home.py --sdk data/e84_bsp_src --assets data/e84_home_assets --weather
python firmware/e84_static_probe/build_with_scons.py --project data/e84_bsp_src/projects/TCM_Home_Weather --gcc-bin <ARM-GCC-bin> --scons-package <SCons包所在目录>
```

天气版构建会读取 `data/e84_home_assets/weather_credentials.local.json`，并把其中的凭据编入固件作为启动默认值。设备重启后自动连接热点并请求天气。这个本地文件和含凭据的固件应仅在本机使用。更换热点或密钥后修改本地文件并重新构建烧录，即可更新启动默认值。配置字段如下。

```json
{
  "wifi_ssid": "热点名",
  "wifi_password": "热点密码",
  "qweather_host": "专属 API Host",
  "qweather_key": "API Key"
}
```

省略本地配置文件时，固件启动后可从串口配置。天气版串口默认关闭输入回显，下列命令也可以临时覆盖固件默认凭据。重新启动会恢复编译时的凭据和自动定位模式。

```text
home_weather_wifi <热点名> <热点密码>
home_weather_api <和风天气API-Host> <API-Key>
home_weather_city auto
```

2026-11-05 烧录 M55 天气版并校验 1,669,012 字节。实测 2.4 GHz 热点 DHCP、北京和上海的和风天气实况及三天预报，触屏切换到上海后核对了详情页。联调时首次 DNS 解析失败后自动恢复，修正版增加短重试，重新上板后两座城市均取数成功。首页帧缓冲显示上海天气卡、室内温湿度、月历和每日推荐。原首页备份位于 `runs/e84_home_weather/home_before.bin`，修正版固件、日志和首页截图位于 `runs/e84_home_weather/final`。

同日烧录定位和主题版，回读校验 1,922,400 字节。经热点直连 IP-API 估算为上海，并取得实况和三天预报。随后验证手动选城、触屏切回自动定位，以及在 IP-API 请求冷却期使用 IP.SB 经 HTTPS 定位并更新天气。启动日志确认背光为 65%。板上截图核对了首页和显示「IP-API 估算位置」的天气详情。本次固件、烧录前备份、串口日志和板上截图位于 `runs/e84_home_geo`。

本地凭据版已烧录并回读校验 1,922,776 字节。再次复位后，仅监听串口，确认设备自动连接热点、完成 IP-API 定位并取得实况和三天预报。验证日志位于 `runs/e84_home_credentialed/reboot_uart.log`。

冷启动回归中，旧版断电后显示 RTC 为 2000 年且 Wi-Fi 未连接。单独设置 RTC 即可恢复天气，确认联网前错误依赖了 RTC。修复版已回读校验 1,924,432 字节。板子完全断电 10 秒再上电，自动连接热点后通过阿里云 NTP 恢复北京时间，并完成定位和实况、三天预报更新。这次 IP-API 连接失败，设备改用 IP.SB 取得位置。校时完成后读回的 RTC 与电脑北京时间相差小于 15 秒。记录位于 `runs/e84_home_coldboot/physical_coldboot_uart.log`。

## 今日推荐与生活指数

天气版复用本地和风配置，调用 [生活指数接口](https://dev.qweather.com/docs/api/indices/indices-forecast/) 的 `/v7/indices/1d`，一次查询运动 `1`、穿衣 `3`、舒适度 `8` 三项。这个接口使用 `经度,纬度`，与已有天气接口的 `纬度/经度` 不同，设备请求时进行转换。只接收日期与设备当前日期一致、类别和等级合法的记录，空文字使用类别名，畸形或过长文字不显示。

首页优先展示运动不适宜、穿衣偏冷偏热、舒适度较差等建议；没有这些情况时按日期轮换展示已返回的建议。卡片最多显示两行，点击进入详情页阅读三项完整建议。详情可以手动刷新，并标明城市、日期和数据提供方。

缓存保存在 RAM，以日期和坐标校验有效性。同一地点当天成功获取后，常规天气更新不会重复请求生活指数。每分钟检查日期，换日或切换城市后重新获取；手动刷新也会重新请求。网络请求失败时约一分钟后重试，已有的同日同地点缓存可以继续显示；没有有效缓存时使用本地提示。完全断电会清空此缓存，上电联网后重新获取。天气原有的 20 分钟刷新独立计时。

动态文案使用覆盖基本汉字区的 20px 字库，继续采用暖米色、浅绿和墨绿配色。图鉴的两部分标题和简化目录也保留在同一固件中。

```text
home_indices_status
home_recommend_show
home_recommend_show home
```

`home_indices_status` 输出缓存的日期、坐标、有效性和三项内容；`TCM_INDICES_READY` 表示成功取数，`TCM_RECOMMEND_RENDER ... usable=1` 表示界面采用生活指数建议。电脑端接口验证和板上记录保存在 `runs/e84_home_indices`。

2026-11-11 使用原有项目密钥实测接口 HTTP 200，板上分别取得上海、北京以及自动定位坐标的三项建议。切换城市后，旧缓存立即变为无效，随后由新城市的数据替换。最终固件完整读回校验 7,203,340 字节，图鉴 CRC 校验通过；超过 64 秒的缓存观察中，版本保持为 1，没有再次获取同日同地点的建议。已读取实屏确认首页两行预览和三项完整详情，最终固件及截图位于 `runs/e84_home_indices/final`，校验记录为 `board_check.json`。
