# Edgi-Talk 调试辅助脚本

这里保留项目开发期间编写的串口、Wi-Fi 和 M55 烧录实验脚本。它们对应文档中已记录的具体实验，部分脚本依赖固定运行目录、哈希、内存布局和旧版固件，并非通用的最新版首页安装程序。执行前检查目标工程、地址范围和所需本地资源。

- `capture_uart.ps1`、`home_uart.ps1`：串口日志及首页校时，当前端口为 COM3、115200 8N1。
- `run_batch.py`：20 样本静态推理实验、HEX 边界检查与 OpenOCD 调用。
- `home_flash.py`：早期首页的烧录与校验实验。
- `wifi_check.py`、`wifi_*uart.ps1`、`wifi_weather_exchange.ps1`、`weather_wire.c`：历史 Wi-Fi 和天气传输验证。

运行涉及硬件的 Python 脚本前，在当前 PowerShell 会话指定目标板的 KitProg3 序列号；可用 `TCM_ARM_GCC_BIN` 覆盖 ARM 工具链 bin 目录。

```powershell
$env:TCM_KITPROG_SERIAL = '<目标板的KitProg3序列号>'
$env:TCM_ARM_GCC_BIN = '<ARM-GCC-bin>'
```

OpenOCD 解压到 `data/e84_programmer/openocd-5.20.0/openocd`，厂商 BSP 放在 `data/e84_bsp_src`。这些第三方工具、BSP 和 `runs/` 中的实验资源不随仓库上传。Wi-Fi 探针从 `TCM_WIFI_CHECK_SSID`、`TCM_WIFI_CHECK_KEY` 等进程环境变量读取配置；实际密码、密钥和含凭据的固件不应提交到仓库。

当前首页和图鉴的构建流程见 [首页说明](../../docs/e84-home.md) 与 [药材图鉴](../../docs/herb-atlas.md)。这些脚本在本次上传时仅作源码整理，没有执行硬件擦写。
