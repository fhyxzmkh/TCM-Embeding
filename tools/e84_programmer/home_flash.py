"""Check the staged homepage image, back up the affected sectors, and flash M55."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

from run_batch import FLASH_BASE, ROOT, SECTOR, TOOLS, hex_bytes, openocd


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


project = ROOT / "data/e84_bsp_src/projects/TCM_Home"
out = ROOT / "runs/e84_home"
out.mkdir(exist_ok=True)
image = out / "tcm_home_cm55.hex"
elf = out / "tcm_home_cm55.elf"
prior_image = ROOT / "runs/e84_u55_224/board_probe_20_ram/tcm_static_probe_20_cm55.hex"
assert digest(prior_image) == "95bc3126fd3fbd4baffe6f9beea14ff9d0c6b8fef0e8a217d0e6205b971f87dc"
shutil.copyfile(project / "rtthread.hex", image)
shutil.copyfile(project / "rt-thread.elf", elf)
shutil.copyfile(project / "rtthread.map", out / "tcm_home_cm55.map")
attributes = subprocess.check_output([TOOLS / "arm-none-eabi-readelf.exe", "-A", elf], text=True)
assert 'Tag_CPU_name: "cortex-m55"' in attributes
data, touched, start, end, count = hex_bytes(image)
erase_end = (end + SECTOR - 1) & ~(SECTOR - 1)
assert FLASH_BASE < erase_end <= 0x607D0000
backup = out / "previous_m55_affected_sectors.bin"
size = erase_end - FLASH_BASE
if not backup.exists():
    openocd(f"init; targets cat1d.sys; dump_image {{{backup.as_posix()}}} {hex(FLASH_BASE)} {hex(size)}; shutdown",
            out / "backup_m55.log", 45, attach=True)
previous, previous_mask, _, _, _ = hex_bytes(prior_image)
old = backup.read_bytes()
assert len(old) == size and old[:0x400] == bytes([255]) * 0x400
assert all(value == previous[i] for i, value in enumerate(old) if previous_mask[i])
report = {
    "image_sha256": digest(image), "hex_data_bytes": count,
    "hex_start": hex(start), "hex_end_exclusive": hex(end),
    "erase_start": hex(FLASH_BASE), "erase_end_exclusive": hex(erase_end),
    "previous_affected_sectors_sha256": digest(backup),
}
(out / "flash_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2), flush=True)

ready, stop, uart = [out / name for name in ("home_uart.ready", "home_uart.stop", "boot_uart.log")]
with (out / "capture_process.log").open("xb") as serial_log:
    capture = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / "tools/e84_programmer/capture_uart.ps1"), "-LogPath", str(uart),
         "-ReadyPath", str(ready), "-StopPath", str(stop)],
        stdin=subprocess.DEVNULL, stdout=serial_log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        for _ in range(100):
            if ready.exists():
                break
            if capture.poll() is not None:
                raise RuntimeError("COM3 capture process exited")
            time.sleep(0.1)
        else:
            raise RuntimeError("COM3 capture did not start")
        result = openocd(
            f"init; reset init; targets cat1d.cm33; flash probe 2; flash write_image erase {{{image.as_posix()}}}; "
            f"verify_image {{{image.as_posix()}}}; reset run; shutdown",
            out / "flash_cm55.log", 100,
        )
        assert f"verified {count} bytes" in result
        for _ in range(100):
            logs = uart.read_bytes()
            if b"TCM_HOME_FIRST_FRAME" in logs and b"TCM_HOME_AHT20" in logs:
                break
            time.sleep(0.1)
        print(uart.read_bytes().decode("ascii", errors="replace"), flush=True)
    finally:
        stop.write_text("stop", encoding="ascii")
        capture.wait(timeout=5)
