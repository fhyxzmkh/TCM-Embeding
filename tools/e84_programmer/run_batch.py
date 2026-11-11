"""Local helper for the authorized E84 static batch run."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tcm_herb.e84_probe import load_assets, sha256, PACK, PLACEMENT

OUT = ROOT / 'runs/e84_u55_224/board_probe_20_ram'
PROJECT = ROOT / 'data/e84_bsp_src/projects/TCM_Static_Probe_20_RAM'
TOOLS = Path(os.environ.get("TCM_ARM_GCC_BIN", r"E:/workspace/Tools/arm-gnu-toolchain-13.3.rel1-mingw-w64-i686-arm-none-eabi/bin"))
BASE = ROOT / 'data/e84_programmer/openocd-5.20.0/openocd'
FLM = ROOT / 'data/e84_bsp_src/projects/libs/TARGET_APP_KIT_PSE84_EVAL_EPC2/config/GeneratedSource/PSE84_SMIF.FLM'
FLASH_BASE, FLASH_SIZE = PLACEMENT['FLASH']
SECTOR = 0x10000
CHECK = OUT / 'build_check.json'
IMAGE = OUT / 'tcm_static_probe_20_cm55.hex'
BACKUP = OUT / 'previous_m55_affected_sectors.bin'
SAMPLES = OUT / 'samples.bin'


def hex_bytes(path):
    image, touched = bytearray(FLASH_SIZE), bytearray(FLASH_SIZE)
    upper, start, end, count, eof = 0, FLASH_BASE + FLASH_SIZE, 0, 0, False
    for line in path.read_text(encoding='ascii').splitlines():
        if not line.startswith(':') or eof:
            raise ValueError('Invalid HEX framing')
        record = bytes.fromhex(line[1:]); offset = int.from_bytes(record[1:3], 'big')
        kind, data = record[3], record[4:-1]
        if len(record) != record[0] + 5 or sum(record) % 256:
            raise ValueError('HEX length or checksum error')
        if kind == 0:
            address = upper + offset
            relative = address - FLASH_BASE
            stop = relative + len(data)
            if relative < 0 or stop > FLASH_SIZE or any(touched[relative:stop]):
                raise ValueError('HEX writes outside M55 region or overlaps itself')
            image[relative:stop] = data; touched[relative:stop] = b'\x01' * len(data)
            start, end = min(start, address), max(end, address + len(data))
            count += len(data)
        elif kind == 4 and len(data) == 2:
            upper = int.from_bytes(data, 'big') << 16
        elif kind == 1 and not data:
            eof = True
        elif kind not in (3, 5) or len(data) != 4:
            raise ValueError(f'Unexpected HEX record: {kind}')
    if not eof or start != FLASH_BASE + 0x400:
        raise ValueError('Unexpected M55 entry placement')
    return image, touched, start, end, count


def verify_build():
    elf, hex_file = PROJECT / 'rt-thread.elf', PROJECT / 'Debug/rtthread.hex'
    def run(command, *args):
        return subprocess.check_output([str(TOOLS / f'arm-none-eabi-{command}.exe'), *map(str, args)], encoding='utf-8')
    attrs, sections, nm = run('readelf', '-A', elf), run('objdump', '-h', elf), run('nm', '-S', '-C', elf)
    assert 'Tag_CPU_name: "cortex-m55"' in attrs and re.search(r'\s\.cy_ml_model_data\s+00000000\b', sections)
    def symbol(name):
        match = re.search(r'^([0-9a-f]+)\s+([0-9a-f]+)\s+[a-zA-Z]\s+' + re.escape(name) + '$', nm, re.M)
        assert match, name
        return tuple(int(x, 16) for x in match.groups())
    for name in ('tflite::Register_ETHOSU()', 'ethosu_invoke_v3', 'tcm_run_probe'):
        symbol(name)
    arena, arena_size = symbol('(anonymous namespace)::arena')
    ram, ram_size = PLACEMENT['SRAM']
    assert ram <= arena and arena + arena_size <= ram + ram_size and arena_size == 1792 * 1024
    data, touched, start, end, count = hex_bytes(hex_file)
    _, _, model, inputs, _ = load_assets(PACK, 20)
    address, size = symbol('tcm_probe_model')
    offset = address - FLASH_BASE
    assert address % 32 == 0 and offset >= 0 and size == len(model)
    assert all(touched[offset:offset + size]) and data[offset:offset + size] == model
    assert ' tcm_probe_input\n' not in nm
    erase_start, erase_end = start & ~(SECTOR - 1), (end + SECTOR - 1) & ~(SECTOR - 1)
    assert erase_start == FLASH_BASE and erase_end <= 0x60800000
    shutil.copyfile(elf, OUT / 'tcm_static_probe_20_cm55.elf')
    shutil.copyfile(hex_file, IMAGE)
    shutil.copyfile(PROJECT / 'rtthread.map', OUT / 'tcm_static_probe_20_cm55.map')
    SAMPLES.write_bytes(inputs)
    (OUT / 'sections.txt').write_text(attrs + '\n' + sections, encoding='utf-8')
    report = {'sha256': sha256(IMAGE.read_bytes()), 'model_sha256': sha256(model), 'inputs_sha256': sha256(inputs),
              'sample_count': 20, 'hex_data_bytes': count, 'hex_start': hex(start), 'hex_end_exclusive': hex(end),
              'erase_start': hex(erase_start), 'erase_end_exclusive': hex(erase_end), 'arena_reserved_bytes': arena_size}
    CHECK.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)
    return report


def checked_build():
    r = json.loads(CHECK.read_text(encoding='utf-8'))
    assert sha256(IMAGE.read_bytes()) == r['sha256']
    _, _, start, end, count = hex_bytes(IMAGE)
    assert r['hex_data_bytes'] == count and int(r['hex_start'], 16) == start and int(r['hex_end_exclusive'], 16) == end
    assert int(r['erase_start'], 16) == FLASH_BASE and int(r['erase_end_exclusive'], 16) == (end + SECTOR - 1) & ~(SECTOR - 1)
    assert end <= 0x60800000 and sha256(SAMPLES.read_bytes()) == r['inputs_sha256']
    return r


def openocd(cmd, log, timeout, *, attach=False):
    serial = os.environ.get('TCM_KITPROG_SERIAL', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', serial):
        raise ValueError('Set TCM_KITPROG_SERIAL to the intended board serial before accessing hardware')
    config = f'set QSPI_FLASHLOADER {{{FLM.as_posix()}}}; set SMIF_BANKS {{ 0 {{addr 0x60000000 size 0x01000000}} }}'
    args = [str(BASE/'bin/openocd.exe'), '-s', str(BASE/'scripts'), '-f', 'interface/kitprog3.cfg',
            '-c', f'adapter serial {serial}', '-c', config,
            *(['-c', 'set ENABLE_ACQUIRE 0'] if attach else []), '-f', 'target/infineon/pse84xgxs2.cfg',
            '-c', 'gdb_port disabled; tcl_port disabled; telnet_port disabled', '-c', cmd]
    with log.open('xb') as stream:
        p = subprocess.run(args, cwd=BASE/'bin', stdin=subprocess.DEVNULL, stdout=stream,
                           stderr=subprocess.STDOUT, timeout=timeout)
    result = log.read_text(errors='replace')
    print(result[-2400:], flush=True)
    p.check_returncode()
    return result


def check_backup(report):
    b = BACKUP.read_bytes()
    assert len(b) == int(report['erase_end_exclusive'], 16) - FLASH_BASE
    assert all(x == 255 for x in b[:1024]), 'Nonblank reserved boot header'
    old = OUT.parent / 'board_probe/tcm_static_probe_cm55.hex'
    prior, touched, _, _, _ = hex_bytes(old)
    assert all(b[i] == prior[i] for i in range(len(b)) if touched[i]), 'Unexpected board firmware'
    print(f'Backup validated: {len(b)} bytes, SHA256 {sha256(b)}.', flush=True)


def backup():
    report = checked_build()
    assert not BACKUP.exists()
    size = int(report['erase_end_exclusive'], 16) - FLASH_BASE
    existing = OUT.parent / 'board_probe_20/previous_m55_affected_sectors.bin'
    BACKUP.write_bytes(existing.read_bytes()[:size])
    check_backup(report)


def flash():
    report = checked_build()
    check_backup(report)
    ready, stop, uart = (OUT / name for name in ('uart.ready', 'uart.stop', 'uart-flash.log'))
    log = OUT / 'flash_cm55.log'
    assert not any(p.exists() for p in (ready, stop, uart, log))
    with (OUT/'uart_capture_process.log').open('xb') as serial_log:
        capture = subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                                    str(ROOT/'tools/e84_programmer/capture_uart.ps1'), '-LogPath', str(uart),
                                    '-ReadyPath', str(ready), '-StopPath', str(stop)], stdin=subprocess.DEVNULL,
                                   stdout=serial_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(100):
                if ready.exists(): break
                if capture.poll() is not None: raise RuntimeError('COM3 capture process exited')
                time.sleep(0.1)
            else: raise RuntimeError('COM3 capture did not start')
            print('COM3 capture ready; starting the checked M55-only image write.', flush=True)
            cmd = (f'init; reset init; targets cat1d.cm33; flash probe 2; flash write_image erase {{{IMAGE.as_posix()}}}; '
                   f'verify_image {{{IMAGE.as_posix()}}}; reset run; shutdown')
            result = openocd(cmd, log, 218)
            if f"verified {report['hex_data_bytes']} bytes" not in result:
                raise RuntimeError('Flash readback count was not confirmed')
            for _ in range(120):
                current = uart.read_bytes().decode('ascii', errors='replace')
                record = re.search(r'TCM_PROBE_WAIT mailbox=(0x[0-9a-f]+) address=(0x[0-9a-f]+) bytes=(\d+) inputs=([0-9a-f]{64})', current)
                if record: break
                if 'TCM_PROBE_ERROR ' in current: raise RuntimeError(current[-2000:])
                time.sleep(0.1)
            else: raise RuntimeError('No input mailbox announced by the board')
            mailbox, address = (int(record[i], 16) for i in (1, 2))
            assert record[4] == report['inputs_sha256'] and int(record[3]) == SAMPLES.stat().st_size == 3010560
            assert 0x64400000 <= mailbox and mailbox % 32 == 0 and address == mailbox + 32
            assert address + SAMPLES.stat().st_size <= 0x64c00000
            print(f'Board reserved HyperRAM at {hex(address)}; transferring the verified input bundle.', flush=True)
            upload = (f'init; targets cat1d.sys; load_image {{{SAMPLES.as_posix()}}} {hex(address)} bin; '
                      f'verify_image {{{SAMPLES.as_posix()}}} {hex(address)} bin; mww {hex(mailbox)} 0x54434d32; shutdown')
            openocd(upload, OUT/'upload_inputs.log', 100, attach=True)
            for _ in range(110):
                if re.search(rb'TCM_PROBE_(DONE|ERROR)', uart.read_bytes()): break
                time.sleep(0.1)
        finally:
            stop.write_text('stop')
            capture.wait(timeout=10)
    print(uart.read_bytes().decode('ascii', errors='replace').replace('\0', '')[-5500:])


if __name__ == '__main__':
    {'verify': verify_build, 'backup': backup, 'flash': flash}[sys.argv[1]]()
