"""Run the temporary Wi-Fi test and restore the known-good M55 homepage."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from run_batch import FLASH_BASE, ROOT, SECTOR, TOOLS, hex_bytes, openocd


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


home_dir = ROOT / "runs/e84_home"
passive = os.environ.get("TCM_WIFI_RUN") == "au_passive"
run_name = os.environ.get("TCM_WIFI_RUN", "")
weather = run_name.startswith("au_weather")
limited = weather or run_name.startswith("au_link")
project_name = ("TCM_WiFi_AU_Weather" if weather else "TCM_WiFi_AU_Link" if limited else
                "TCM_WiFi_AU_Passive" if passive else "TCM_WiFi_Check")
project = ROOT / "data/e84_bsp_src/projects" / project_name
out = ROOT / "runs/e84_wifi_check" / os.environ.get("TCM_WIFI_RUN", "")
out.mkdir(exist_ok=True)
home = home_dir / "tcm_home_cm55.hex"
home_report = json.loads((home_dir / "flash_check.json").read_text())
assert digest(home) == home_report["image_sha256"]
assert digest(home) == "a6313b3f53f8b534c3bcfe30d8da3474c55d9e9b8b6bbce6f90200c76dfdb135"
_, _, _, home_end, home_count = hex_bytes(home)
wifi = out / "tcm_wifi_check_cm55.hex"
shutil.copyfile(project / "Debug/rtthread.hex", wifi)
attributes = subprocess.check_output([TOOLS / "arm-none-eabi-readelf.exe", "-A", project / "rt-thread.elf"], text=True)
assert 'Tag_CPU_name: "cortex-m55"' in attributes
image, used, _, wifi_end, wifi_count = hex_bytes(wifi)
assert (b"WIFI_PASSIVE_START channels=1-13 probes=0" if passive else b"WIFI_CHECK_TCP") in image
if passive:
    assert b"WIFI_CHECK_TCP" not in image
if limited:
    assert b"WIFI_PASSIVE_START channels=1-13 probes=0" in image
    assert b"WIFI_LIMIT_BAND" in image and b"WIFI_LIMIT_TX" in image
if weather:
    assert b"WEATHER_TLS_OK" in image and b"WEATHER_WIRE_END" in image
assert b"Mount filesystem failed, try to mkfs" not in image
sector_end = (home_end + SECTOR - 1) & ~(SECTOR - 1)
assert FLASH_BASE < wifi_end <= sector_end == 0x60640000
before = out / "home_before.bin"
size = sector_end - FLASH_BASE
openocd(f"init; targets cat1d.sys; dump_image {{{before.as_posix()}}} {hex(FLASH_BASE)} {hex(size)}; shutdown",
        out / "backup_home.log", 45, attach=True)
expected, mask, _, _, _ = hex_bytes(home)
dump = before.read_bytes()
assert len(dump) == size and dump[:0x400] == bytes([255]) * 0x400
assert all(value == (expected[i] if mask[i] else 255) for i, value in enumerate(dump))

serial_env = dict(os.environ)
if not passive:
    credentials = sys.stdin.read().splitlines()
    assert len(credentials) == (3 if weather else 2) and all(credentials)
    serial_env.update(TCM_WIFI_CHECK_SSID=credentials[0], TCM_WIFI_CHECK_KEY=credentials[1])
    if weather:
        serial_env["TCM_WIFI_WEATHER_KEY"] = credentials[2]


def flash(path, count, log):
    result = openocd(
        f"init; reset init; targets cat1d.cm33; flash probe 2; flash write_image erase {{{path.as_posix()}}}; "
        f"verify_image {{{path.as_posix()}}}; reset run; shutdown", out / log, 100)
    assert f"verified {count} bytes" in result


try:
    ready, stop, uart = (out / name for name in ("uart.ready", "uart.stop", "wifi_boot.log"))
    capture = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / "tools/e84_programmer/capture_uart.ps1"), "-LogPath", str(uart),
         "-ReadyPath", str(ready), "-StopPath", str(stop)],
        stdin=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if ready.exists():
                break
            if capture.poll() is not None:
                raise RuntimeError("COM3 capture exited")
            time.sleep(0.1)
        else:
            raise RuntimeError("COM3 capture not ready")
        flash(wifi, wifi_count, "flash_wifi.log")
        time.sleep(3)
    finally:
        stop.write_text("stop", encoding="ascii")
        capture.wait(timeout=5)
    print(uart.read_bytes().decode("ascii", errors="replace"), flush=True)
    script = "wifi_passive_uart.ps1" if passive else ("wifi_limited_uart.ps1" if limited else "wifi_uart.ps1")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "tools/e84_programmer" / script),
         "-LogPath", str(out / "wifi_uart.log")], env=serial_env, stdin=subprocess.DEVNULL, timeout=150 if weather else 90)
    result.check_returncode()
finally:
    flash(home, home_count, "restore_home.log")
    time.sleep(2)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "tools/e84_programmer/home_uart.ps1"),
         "-LogPath", str(out / "home_restored_uart.log")], stdin=subprocess.DEVNULL, check=True, timeout=12)
    after = out / "home_after.bin"
    openocd(f"init; targets cat1d.sys; dump_image {{{after.as_posix()}}} {hex(FLASH_BASE)} {hex(size)}; shutdown",
            out / "verify_home_exact.log", 45, attach=True)
    assert after.read_bytes() == dump
    print("Homepage flash region matches the pre-test backup byte-for-byte.", flush=True)
