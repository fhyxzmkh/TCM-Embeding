#include <rtconfig.h>
#ifdef TCM_HOME_WEATHER
#include <lvgl.h>
#include <rtthread.h>
#include <rthw.h>
#include <finsh.h>
#include <string.h>
#include "home_weather.h"
#include "home_recommend.h"

LV_FONT_DECLARE(home_font);

#define INK 0x273B32
#define FOREST 0x304C40
#define MUTED 0x526559
#define PAPER 0xE8E6DA
#define SURFACE 0xF3F0E5
#define SAGE 0xDEE7D8

static lv_obj_t *panel, *scroll, *preview_label, *date_label, *source_label;
static home_indices_t snapshot;
static int requested_page = -1;

static const char *index_name(unsigned type)
{
    return type == 1 ? "运动" : type == 3 ? "穿衣" : "舒适度";
}

static lv_obj_t *text_label(lv_obj_t *parent, int x, int y, int width, uint32_t color, const char *text)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_pos(label, x, y);
    lv_obj_set_width(label, width);
    lv_obj_set_style_text_font(label, &home_font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    lv_obj_set_style_text_line_space(label, 5, 0);
    lv_label_set_text(label, text);
    return label;
}

static void close_clicked(lv_event_t *event)
{
    (void)event;
    lv_obj_add_flag(panel, LV_OBJ_FLAG_HIDDEN);
}

static void open_clicked(lv_event_t *event)
{
    (void)event;
    home_recommend_refresh();
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_HIDDEN);
    rt_kprintf("TCM_RECOMMEND_VIEW open=1\n");
}

static void refresh_clicked(lv_event_t *event)
{
    (void)event;
    home_weather_refresh();
}

static void navigation_button(int x, const char *text, lv_event_cb_t callback)
{
    lv_obj_t *button = lv_button_create(panel);
    lv_obj_set_pos(button, x, 20);
    lv_obj_set_size(button, 128, 52);
    lv_obj_set_style_bg_color(button, lv_color_hex(FOREST), 0);
    lv_obj_set_style_radius(button, 14, 0);
    lv_obj_set_style_shadow_width(button, 0, 0);
    lv_obj_t *label = text_label(button, 0, 0, 116, SURFACE, text);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(label);
    lv_obj_add_event_cb(button, callback, LV_EVENT_CLICKED, NULL);
}

static void advice_card(const char *name, const char *category, const char *text)
{
    lv_obj_t *card = lv_obj_create(scroll);
    lv_obj_set_width(card, 432);
    lv_obj_set_height(card, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(card, lv_color_hex(SURFACE), 0);
    lv_obj_set_style_border_width(card, 0, 0);
    lv_obj_set_style_radius(card, 16, 0);
    lv_obj_set_style_pad_all(card, 18, 0);
    lv_obj_set_style_pad_row(card, 12, 0);
    lv_obj_remove_flag(card, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_t *heading = text_label(card, 0, 0, 396, FOREST, "");
    lv_label_set_text_fmt(heading, "%s%s%s", name, *category ? "  " : "", category);
    lv_obj_set_style_bg_color(heading, lv_color_hex(SAGE), 0);
    lv_obj_set_style_bg_opa(heading, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(heading, 8, 0);
    lv_obj_set_style_radius(heading, 8, 0);
    text_label(card, 0, 0, 396, INK, *text ? text : category);
}

static unsigned preview_index(home_date_t date)
{
    for (unsigned i = 0; i < snapshot.count; ++i)
        if (snapshot.items[i].type == 1 && snapshot.items[i].level == 3) return i;
    for (unsigned i = 0; i < snapshot.count; ++i)
        if (snapshot.items[i].type == 3 && (snapshot.items[i].level <= 3 || snapshot.items[i].level >= 6)) return i;
    for (unsigned i = 0; i < snapshot.count; ++i)
        if (snapshot.items[i].type == 8 && snapshot.items[i].level >= 3) return i;
    return (date.month * 31 + date.day) % snapshot.count;
}

void home_recommend_refresh(void)
{
    if (!panel) return;
    static unsigned shown_revision = (unsigned)-1, shown_year, shown_month, shown_day;
    static bool shown_valid;
    static char shown_coordinates[20], shown_city[64];
    home_weather_t weather;
    home_date_t now;
    home_weather_get(&weather);
    home_indices_get(&snapshot);
    bool clock_valid = home_clock_get(&now);
    const char *coordinates = weather.location.coordinates[0] ? weather.location.coordinates : home_cities[weather.city].coordinates;
    const char *city = weather.location.coordinates[0] ? weather.location.name : home_cities[weather.city].name;
    if (!*city) city = "网络位置";
    bool usable = home_indices_usable(&snapshot, now, coordinates);
    if (shown_revision != snapshot.revision || shown_year != now.year || shown_month != now.month ||
        shown_day != now.day || shown_valid != clock_valid || strcmp(shown_coordinates, coordinates) || strcmp(shown_city, city)) {
        shown_revision = snapshot.revision;
        shown_year = now.year;
        shown_month = now.month;
        shown_day = now.day;
        shown_valid = clock_valid;
        strcpy(shown_coordinates, coordinates);
        strcpy(shown_city, city);
        lv_obj_clean(scroll);
        if (usable) {
            const home_index_t *chosen = &snapshot.items[preview_index(now)];
            lv_label_set_text_fmt(preview_label, "%s：%s", index_name(chosen->type),
                                  chosen->text[0] ? chosen->text : chosen->category);
            for (unsigned i = 0; i < snapshot.count; ++i)
                advice_card(index_name(snapshot.items[i].type), snapshot.items[i].category, snapshot.items[i].text);
            lv_label_set_text_fmt(date_label, "%s  %u月%u日", city, now.month, now.day);
            lv_label_set_text(source_label, "和风天气");
        } else {
            bool on_term;
            const char *local = home_daily_tip(now, home_solar_term(now, &on_term));
            lv_label_set_text(preview_label, local);
            advice_card("日常提醒", "", local);
            if (clock_valid) lv_label_set_text_fmt(date_label, "%u月%u日", now.month, now.day);
            else lv_label_set_text(date_label, "日期待校时");
            lv_label_set_text(source_label, "");
        }
        lv_obj_scroll_to_y(scroll, 0, LV_ANIM_OFF);
        rt_kprintf("TCM_RECOMMEND_RENDER indices=%u usable=%u\n", snapshot.count, (unsigned)usable);
    }
    rt_base_t irq = rt_hw_interrupt_disable();
    int page = requested_page;
    requested_page = -1;
    rt_hw_interrupt_enable(irq);
    if (page == 1) {
        lv_obj_remove_flag(panel, LV_OBJ_FLAG_HIDDEN);
        rt_kprintf("TCM_RECOMMEND_VIEW open=1 indices=%u usable=%u\n", snapshot.count, (unsigned)usable);
    } else if (page == 0) lv_obj_add_flag(panel, LV_OBJ_FLAG_HIDDEN);
}

void home_recommend_create(void *screen, void *card, void *preview)
{
    preview_label = preview;
    lv_obj_set_height(preview_label, 66);
    lv_label_set_long_mode(preview_label, LV_LABEL_LONG_DOT);
    lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(card, open_clicked, LV_EVENT_CLICKED, NULL);
    text_label(card, 378, 12, 20, MUTED, ">");
    panel = lv_obj_create(screen);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 480, 800);
    lv_obj_set_style_bg_color(panel, lv_color_hex(PAPER), 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(panel, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_HIDDEN);
    navigation_button(24, "返回首页", close_clicked);
    navigation_button(328, "刷新建议", refresh_clicked);
    text_label(panel, 178, 31, 140, INK, "今日推荐");
    date_label = text_label(panel, 26, 91, 428, MUTED, "");
    lv_obj_set_height(date_label, 32);
    lv_label_set_long_mode(date_label, LV_LABEL_LONG_DOT);
    scroll = lv_obj_create(panel);
    lv_obj_remove_style_all(scroll);
    lv_obj_set_pos(scroll, 24, 139);
    lv_obj_set_size(scroll, 432, 601);
    lv_obj_set_style_pad_row(scroll, 16, 0);
    lv_obj_set_flex_flow(scroll, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_scroll_dir(scroll, LV_DIR_VER);
    source_label = text_label(panel, 26, 749, 428, MUTED, "");
    lv_obj_set_style_text_align(source_label, LV_TEXT_ALIGN_RIGHT, 0);
}

static int home_recommend_show(int argc, char **argv)
{
    if (argc > 2 || !panel || (argc == 2 && strcmp(argv[1], "home"))) return -RT_EINVAL;
    rt_base_t irq = rt_hw_interrupt_disable();
    requested_page = argc == 2 ? 0 : 1;
    rt_hw_interrupt_enable(irq);
    return 0;
}
MSH_CMD_EXPORT(home_recommend_show, open daily recommendation or return home);
#endif
