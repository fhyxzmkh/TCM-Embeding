"""Stage the homepage in a separate project in the Edgi-Talk SDK."""

import argparse
import csv
from datetime import date
import json
from pathlib import Path
import re
import shutil

from PIL import ImageFont
from prepare_atlas import stage_atlas


SOURCE = Path(__file__).resolve().parent
TERMS = (
    "小寒 大寒 立春 雨水 驚蟄 春分 清明 穀雨 立夏 小滿 芒種 夏至 "
    "小暑 大暑 立秋 處暑 白露 秋分 寒露 霜降 立冬 小雪 大雪 冬至"
).split()


def term_table(assets):
    rows = []
    for year in range(2025, 2036):
        table = (assets / f"T{year}c.txt").read_text(encoding="utf-8-sig")
        entries = re.findall(
            r"^(\d{4})年(\d+)月(\d+)日[^\n]*?星期[一二三四五六日][ \t]+(\S+)[ \t]*$",
            table, flags=re.MULTILINE,
        )
        if [term for _, _, _, term in entries] != TERMS:
            raise ValueError(f"Unexpected solar terms in the {year} HKO calendar")
        days = []
        for index, (entry_year, month, day, _) in enumerate(entries):
            observed = date(int(entry_year), int(month), int(day))
            if observed.year != year or observed.month != index // 2 + 1:
                raise ValueError(f"Unexpected term date {observed}")
            days.append(observed.day)
        rows.append("    {" + ", ".join(map(str, days)) + f"}}, /* {year} */")
    return (
        "/* Hong Kong Observatory Gregorian-Lunar Calendar Conversion Table. */\n"
        "#define HOME_TERM_FIRST_YEAR 2025\n#define HOME_TERM_LAST_YEAR 2035\n"
        "static const uint8_t home_term_days[][24] = {\n" + "\n".join(rows) + "\n};\n"
    )


def font_source(assets, sources, extra_glyphs="", *, size=20, name="home_font", ascii_glyphs=None):
    chars = {chr(code) for code in range(32, 127)} if ascii_glyphs is None else set(ascii_glyphs)
    chars.update(extra_glyphs)
    for source in sources:
        chars.update(c for c in source.read_text(encoding="utf-8") if ord(c) > 127)
    chars = sorted(chars)
    font = ImageFont.truetype(str(assets / "NotoSansCJKsc-Regular.otf"), size=size)
    bitmap = bytearray()
    glyphs = ["    {0},"]
    for char in chars:
        mask, (x, y) = font.getmask2(char, mode="L", anchor="ls")
        width, height = mask.size
        advance = round(font.getlength(char) * 16)
        glyphs.append(
            f"    {{.bitmap_index={len(bitmap)}, .adv_w={advance}, "
            f".box_w={width}, .box_h={height}, .ofs_x={x}, .ofs_y={-y-height}}},"
        )
        pixels = [(value * 15 + 127) // 255 for value in mask]
        if len(pixels) % 2:
            pixels.append(0)
        bitmap.extend((pixels[i] << 4) | pixels[i + 1] for i in range(0, len(pixels), 2))
    pixels = "\n".join(
        "    " + " ".join(f"0x{value:02x}," for value in bitmap[i:i + 16])
        for i in range(0, len(bitmap), 16)
    )
    start = ord(chars[0])
    offsets = [ord(c) - start for c in chars]
    unicode_list = "\n".join(
        "    " + ", ".join(map(str, offsets[i:i + 12])) + ","
        for i in range(0, len(offsets), 12)
    )
    ascent, descent = font.getmetrics()
    descriptors = "\n".join(glyphs)
    return f"""/* Rasterized subset of Noto Sans CJK SC, copyright 2014-2021 Adobe.
 * SIL Open Font License 1.1. See OFL.txt.
 */
#include <lvgl.h>

static const uint8_t bitmap[] = {{
{pixels}
}};
static const lv_font_fmt_txt_glyph_dsc_t glyphs[] = {{
{descriptors}
}};
static const uint16_t unicode_list[] = {{
{unicode_list}
}};
static const lv_font_fmt_txt_cmap_t cmap = {{
    .range_start = {start}, .range_length = {offsets[-1] + 1}, .glyph_id_start = 1,
    .unicode_list = unicode_list, .list_length = {len(chars)}, .type = LV_FONT_FMT_TXT_CMAP_SPARSE_TINY,
}};
static const lv_font_fmt_txt_dsc_t font_data = {{
    .glyph_bitmap = bitmap, .glyph_dsc = glyphs, .cmaps = &cmap, .cmap_num = 1, .bpp = 4,
}};
const lv_font_t {name} = {{
    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,
    .line_height = {ascent + descent}, .base_line = {descent}, .dsc = &font_data,
}};
"""


def city_catalog(assets):
    names, aliases = set(), {}
    with (assets / "China-City-List.csv").open(encoding="utf-8-sig", newline="") as cities:
        next(cities)  # Catalog version line before the CSV header.
        for row in csv.DictReader(cities):
            if row["ISO_3166_1"] != "CN":
                continue
            for column in ("Location_Name_ZH", "Adm1_Name_ZH", "Adm2_Name_ZH"):
                names.update(row[column])
            en, zh = row["Adm2_Name_EN"], row["Adm2_Name_ZH"]
            if en and zh and en.isascii():
                aliases.setdefault(en, set()).add(zh.removesuffix("市"))
    rows = [f'    {{{json.dumps(en)}, {json.dumps(next(iter(zh)), ensure_ascii=False)}}},'
            for en, zh in sorted(aliases.items()) if len(zh) == 1]
    return "".join(names), "static const struct { const char *en, *zh; } home_city_aliases[] = {\n" + "\n".join(rows) + "\n};\n"


def weather_defaults(assets):
    path = assets / "weather_credentials.local.json"
    fields = ("wifi_ssid", "wifi_password", "qweather_host", "qweather_key")
    if not path.exists():
        values = dict.fromkeys(fields, "")
    else:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict) or any(not isinstance(values.get(field), str) for field in fields):
            raise ValueError(f"Invalid credential fields in {path.name}")
        ssid, password, host, key = (values[field] for field in fields)
        if not (ssid.isprintable() and 1 <= len(ssid.encode("utf-8")) <= 32 and
                password.isprintable() and 8 <= len(password.encode("utf-8")) <= 32 and
                len(host) <= 80 and re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.qweatherapi\.com", host) and
                re.fullmatch(r"[a-fA-F0-9]{32}", key)):
            raise ValueError(f"Invalid credential values in {path.name}")
    return "\n".join(f"#define HOME_DEFAULT_{field.upper()} {json.dumps(values[field], ensure_ascii=False)}"
                     for field in fields) + "\n"


def stage_weather(sdk, project, assets):
    vendor = sdk / "projects/Edgi_Talk_M55_WIFI"
    libraries = sdk / "libraries/components"
    app = project / "applications"
    (app / "home_weather_defaults.h").write_text(weather_defaults(assets), encoding="utf-8")
    config = project / "rtconfig.h"
    home = config.read_text(encoding="utf-8")
    home = re.sub(r"^#define FINSH_USING_HISTORY\b[^\n]*\n", "", home, flags=re.MULTILINE)
    home = re.sub(r"^#define RT_SERIAL_RB_BUFSZ\b[^\n]*", "#define RT_SERIAL_RB_BUFSZ 2048", home, flags=re.MULTILINE)
    home = re.sub(r"^#define FINSH_CMD_SIZE\b[^\n]*", "#define FINSH_CMD_SIZE 192", home, flags=re.MULTILINE)
    define = re.compile(r"^#define (\w+)\b[^\n]*", re.MULTILINE)
    existing = set(define.findall(home))
    unused = {
        "RT_USING_RYM", "RT_USING_DFS_ROMFS", "RT_USING_FAL", "SAL_INTERNET_CHECK",
        "RT_WLAN_AUTO_CONNECT_ENABLE", "RT_WLAN_CFG_ENABLE", "RT_WLAN_MSH_CMD_ENABLE",
        "FINSH_USING_HISTORY", "WHD_RESOURCES_IN_EXTERNAL_STORAGE_FAL",
        "BSP_USING_FILESYSTEM", "BSP_USING_FLASH", "BSP_USING_SDCARD", "BSP_USING_LITTLEFS", "MEM_SIZE",
    }
    wifi = vendor.joinpath("rtconfig.h").read_text(encoding="utf-8")
    additions = [m[0] for m in define.finditer(wifi)
                 if m[1] not in existing | unused and not m[1].startswith(("FAL_", "LFS_", "PKG_"))]
    additions += [
        "#define TCM_HOME_WEATHER", "#define WHD_RESOURCES_IN_MEMORY", "#define FINSH_ECHO_DISABLE_DEFAULT",
        "#define PKG_USING_MBEDTLS", "#define MBEDTLS_SSL_MAX_CONTENT_LEN 16384", "#define PKG_USING_CJSON",
    ]
    guard = home.rindex("#endif")
    config.write_text(home[:guard] + "\n".join(additions) + "\n\n" + home[guard:], encoding="utf-8")

    packages = project / "packages"
    packages.mkdir(exist_ok=True)
    shutil.copytree(vendor / "resources", project / "resources", dirs_exist_ok=True)
    shutil.copy2(vendor / "packages/SConscript", packages)
    shutil.copytree(vendor / "packages/FreeRTOS_Wrapper-latest", packages / "FreeRTOS_Wrapper-latest", dirs_exist_ok=True)
    xiaozhi = sdk / "projects/Edgi_Talk_M55_XiaoZhi/packages"
    for package in ("mbedtls-v2.28.1", "cJSON-v1.7.17"):
        shutil.copytree(xiaozhi / package, packages / package, dirs_exist_ok=True)
    tls_config = packages / "mbedtls-v2.28.1/ports/inc/tls_config.h"
    tls_text = tls_config.read_text(encoding="utf-8")
    tls_text = re.sub(r"^//\s*#define (MBEDTLS_HAVE_TIME_DATE|MBEDTLS_PLATFORM_TIME_ALT)\b",
                      r"#define \1", tls_text, flags=re.MULTILINE)
    tls_config.write_text(tls_text, encoding="utf-8")

    driver = (libraries / "wifi-host-driver/porting/src/wlan/whd_wlan.c").read_text(encoding="utf-8")
    driver = driver.replace("static const struct rt_wlan_dev_ops ",
        "static rt_err_t home_wifi_join_limited(struct rt_wlan_device *, struct rt_sta_info *);\n\n"
        "static const struct rt_wlan_dev_ops ", 1)
    driver = driver.replace(".wlan_scan = drv_wlan_scan,", ".wlan_scan = NULL,")
    driver = driver.replace(".wlan_join = drv_wlan_join,", ".wlan_join = home_wifi_join_limited,")
    driver = driver.replace(".wlan_softap = drv_wlan_softap,", ".wlan_softap = NULL,")
    (app / "whd_wlan_home.c").write_text(driver + '\n#include "home_wifi_port.inc"\n', encoding="utf-8")
    shutil.copy2(SOURCE / "home_wifi_port.inc", app)

    lv_config = (sdk / "libraries/Common/board/ports/lvgl/lv_conf.h").read_text(encoding="utf-8")
    lv_config, count = re.subn(r"^(\s*#define LV_USE_LODEPNG\s+)0\b", r"\g<1>1", lv_config, flags=re.MULTILINE)
    if count != 1:
        raise ValueError("Unable to enable the LVGL inflate implementation")
    (app / "home_lv_conf.h").write_text(lv_config, encoding="utf-8")
    roots = []
    for variable, name in (("weather_ca", "isrgrootx1.pem"), ("geo_ca", "gtsr4.pem")):
        pem = (assets / name).read_text(encoding="ascii").splitlines()
        roots.append(f"static const unsigned char {variable}[] =\n" +
                     "\n".join(f'    "{line}\\n"' for line in pem) + ";\n")
    (app / "weather_root.h").write_text("\n".join(roots), encoding="ascii")

    construct = project / "SConstruct"
    script = construct.read_text(encoding="utf-8").replace("Export('rtconfig')", "Export('rtconfig')\nExport('env')")
    construct.write_text(script.replace("DoBuilding(TARGET, objs)", """\
env.AppendUnique(CPPDEFINES=[('LV_CONF_PATH', 'home_lv_conf.h')])
objects = Flatten(objs)
original = [node for node in objects if getattr(node, 'name', None) == 'whd_wlan.c']
assert len(original) == 1
objects.remove(original[0])
assert any(getattr(node, 'name', None) == 'whd_wlan_home.c' for node in objects)
DoBuilding(TARGET, objects)"""), encoding="utf-8")


def stage(sdk, assets, weather=False, atlas=None):
    vendor = sdk / "projects/Edgi_Talk_M55_LVGL"
    project_name = "TCM_Home_Atlas" if atlas else "TCM_Home_Weather" if weather else "TCM_Home"
    project = sdk / "projects" / project_name
    shutil.copytree(
        vendor, project, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "applications", "figures", "build", "Debug", ".settings", ".sconsign*",
            ".cproject", ".project", "rt-thread.elf", "rtthread.hex", "rtthread.map",
        ),
    )
    unused = ("LVGL_USING_DEMOS", "BSP_LVGL_DEMO_VIRTUAL3D_EMOJI",
              "PKG_USING_CPU_USAGE", "PKG_USING_CPU_USAGE_LATEST_VERSION")
    config = project / "rtconfig.h"
    config_text = config.read_text(encoding="utf-8")
    for macro in unused:
        config_text = re.sub(rf"^#define {macro}\b[^\n]*\n", "", config_text, flags=re.MULTILINE)
    config.write_text(config_text, encoding="utf-8")
    menuconfig = project / ".config"
    menu_text = menuconfig.read_text(encoding="utf-8")
    for macro in unused:
        menu_text = re.sub(rf"^CONFIG_{macro}=y$", f"# CONFIG_{macro} is not set", menu_text, flags=re.MULTILINE)
    menuconfig.write_text(menu_text, encoding="utf-8")
    app = project / "applications"
    app.mkdir(exist_ok=True)
    sources = sorted(SOURCE.glob("*.c"))
    for path in [*sources, *SOURCE.glob("*.h"), SOURCE / "OFL.txt"]:
        shutil.copy2(path, app / path.name)
    (app / "home_term_days.h").write_text(term_table(assets), encoding="utf-8")
    city_glyphs = ""
    if weather:
        city_glyphs, aliases = city_catalog(assets)
        # Service recommendations contain changing text, including characters absent from the atlas.
        city_glyphs += "".join(chr(code) for code in range(0x4e00, 0xa000))
        (app / "home_city_aliases.h").write_text(aliases, encoding="utf-8")
        (app / "home_numbers.c").write_text(font_source(
            assets, (), "°", size=32, name="home_numbers", ascii_glyphs=" 0123456789-:C"
        ), encoding="utf-8")
    if atlas:
        city_glyphs += stage_atlas(atlas, app)
        config_text = config.read_text(encoding="utf-8")
        guard = config_text.rindex("#endif")
        config.write_text(config_text[:guard] + "#define TCM_HOME_ATLAS\n" + config_text[guard:], encoding="utf-8")
    (app / "home_font.c").write_text(font_source(assets, sources, city_glyphs), encoding="utf-8")
    (app / "SConscript").write_text(
        """from building import *

src = Glob('*.c') + Glob('*.S')
pdl = '#/build/libraries/components/mtb-device-support-pse8xxgp/pdl/drivers/source/'
src += [File(pdl + 'cy_rtc.c')]
if GetDepend('TCM_HOME_WEATHER'):
    src += [File(pdl + name) for name in ('cy_crypto_core_hw.c', 'cy_crypto_core_trng.c')]
group = DefineGroup('Applications', src, depend=[''], CPPPATH=[GetCurrentDir()])
if GetDepend('TCM_HOME_ATLAS'):
    Depends(File('atlas_blob.o'), File('atlas_text.bin'))
Return('group')
""", encoding="utf-8",
    )
    if weather:
        stage_weather(sdk, project, assets)
    return project


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--weather", action="store_true")
    parser.add_argument("--atlas", type=Path, help="Validated atlas directory")
    args = parser.parse_args()
    if args.atlas and not args.weather:
        parser.error("The atlas homepage requires --weather")
    print(stage(args.sdk.resolve(), args.assets.resolve(), args.weather, args.atlas.resolve() if args.atlas else None))
