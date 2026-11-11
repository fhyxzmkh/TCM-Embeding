#include <rtconfig.h>
#ifdef TCM_HOME_ATLAS
#include <lvgl.h>
#include <rtthread.h>
#include <rthw.h>
#include <finsh.h>
#include <stdlib.h>
#include <string.h>
#include "home_atlas.h"

LV_FONT_DECLARE(home_font);

#define ATLAS_INK 0x273B32
#define ATLAS_FOREST 0x304C40
#define ATLAS_MUTED 0x526559
#define ATLAS_PAPER 0xE8E6DA
#define ATLAS_SURFACE 0xF3F0E5
#define ATLAS_SAGE 0xDEE7D8
#define ATLAS_BORDER 0xCCD4C4
#define ROWS_PER_PAGE 6

static lv_obj_t *catalog_panel, *detail_panel, *category_selector, *number_input, *keyboard;
static lv_obj_t *catalog_count, *page_label, *row_buttons[ROWS_PER_PAGE];
static lv_obj_t *row_names[ROWS_PER_PAGE], *row_categories[ROWS_PER_PAGE], *empty_message;
static lv_obj_t *previous_button, *next_button, *detail_title, *answer_scroll;
static uint16_t *matches;
static unsigned match_count, catalog_page;
static int requested_entry = -2;
static unsigned requested_section;

static const char *atlas_string(uint32_t offset)
{
    return home_atlas_blob + offset;
}

static lv_obj_t *atlas_label(lv_obj_t *parent, int x, int y, int width, uint32_t color, const char *text)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_pos(label, x, y);
    lv_obj_set_width(label, width);
    lv_obj_set_style_text_font(label, &home_font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    lv_label_set_text(label, text);
    return label;
}

static lv_obj_t *atlas_panel(lv_obj_t *screen)
{
    lv_obj_t *panel = lv_obj_create(screen);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 480, 800);
    lv_obj_set_style_bg_color(panel, lv_color_hex(ATLAS_PAPER), 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(panel, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_HIDDEN);
    return panel;
}

static lv_obj_t *atlas_button(lv_obj_t *parent, int x, int y, int width, const char *text,
                              lv_event_cb_t callback, void *user_data)
{
    lv_obj_t *button = lv_button_create(parent);
    lv_obj_set_pos(button, x, y);
    lv_obj_set_size(button, width, 52);
    lv_obj_set_style_bg_color(button, lv_color_hex(ATLAS_FOREST), 0);
    lv_obj_set_style_shadow_width(button, 0, 0);
    lv_obj_set_style_radius(button, 14, 0);
    lv_obj_t *label = atlas_label(button, 0, 0, width - 12, ATLAS_SURFACE, text);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(label);
    lv_obj_add_event_cb(button, callback, LV_EVENT_CLICKED, user_data);
    return button;
}

static void style_selector(lv_obj_t *selector)
{
    lv_obj_set_style_bg_color(selector, lv_color_hex(ATLAS_SAGE), 0);
    lv_obj_set_style_border_color(selector, lv_color_hex(ATLAS_BORDER), 0);
    lv_obj_set_style_text_color(selector, lv_color_hex(ATLAS_INK), 0);
    lv_obj_set_style_text_font(selector, &home_font, 0);
    lv_obj_set_style_text_font(selector, &lv_font_montserrat_24, LV_PART_INDICATOR);
    lv_obj_set_style_radius(selector, 12, 0);
    lv_obj_t *list = lv_dropdown_get_list(selector);
    lv_obj_set_style_bg_color(list, lv_color_hex(ATLAS_SURFACE), 0);
    lv_obj_set_style_text_color(list, lv_color_hex(ATLAS_INK), 0);
    lv_obj_set_style_text_font(list, &home_font, 0);
    lv_obj_set_style_bg_color(list, lv_color_hex(ATLAS_FOREST), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_set_style_text_color(list, lv_color_hex(ATLAS_SURFACE), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_set_style_max_height(list, 420, 0);
}

static void catalog_render(void)
{
    unsigned pages = match_count ? (match_count + ROWS_PER_PAGE - 1) / ROWS_PER_PAGE : 1;
    if (catalog_page >= pages) catalog_page = pages - 1;
    lv_label_set_text_fmt(catalog_count, "%u 味药材", match_count);
    lv_label_set_text_fmt(page_label, "%u / %u", catalog_page + 1, pages);
    if (catalog_page == 0) lv_obj_add_state(previous_button, LV_STATE_DISABLED);
    else lv_obj_remove_state(previous_button, LV_STATE_DISABLED);
    if (catalog_page + 1 >= pages) lv_obj_add_state(next_button, LV_STATE_DISABLED);
    else lv_obj_remove_state(next_button, LV_STATE_DISABLED);
    if (match_count) lv_obj_add_flag(empty_message, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_remove_flag(empty_message, LV_OBJ_FLAG_HIDDEN);
    for (unsigned i = 0; i < ROWS_PER_PAGE; ++i) {
        unsigned position = catalog_page * ROWS_PER_PAGE + i;
        if (position >= match_count) {
            lv_obj_add_flag(row_buttons[i], LV_OBJ_FLAG_HIDDEN);
            continue;
        }
        lv_obj_remove_flag(row_buttons[i], LV_OBJ_FLAG_HIDDEN);
        const home_atlas_entry_t *entry = &home_atlas_entries[matches[position]];
        lv_label_set_text_fmt(row_names[i], "%s  %s", atlas_string(entry->code), atlas_string(entry->name));
        lv_label_set_text_static(row_categories[i], atlas_string(home_atlas_categories[entry->category]));
    }
}

static void catalog_filter(lv_event_t *event)
{
    (void)event;
    unsigned category = lv_dropdown_get_selected(category_selector);
    const char *number = lv_textarea_get_text(number_input);
    size_t length = strlen(number);
    match_count = 0;
    for (unsigned i = 0; i < home_atlas_entry_count; ++i) {
        const home_atlas_entry_t *entry = &home_atlas_entries[i];
        if ((!category || entry->category + 1 == category) &&
            (!length || strncmp(atlas_string(entry->code), number, length) == 0))
            matches[match_count++] = (uint16_t)i;
    }
    catalog_page = 0;
    catalog_render();
}

static void catalog_turn(lv_event_t *event)
{
    intptr_t direction = (intptr_t)lv_event_get_user_data(event);
    if (direction < 0 && catalog_page) --catalog_page;
    else if (direction > 0 && (catalog_page + 1) * ROWS_PER_PAGE < match_count) ++catalog_page;
    catalog_render();
}

static void show_home(lv_event_t *event)
{
    (void)event;
    lv_obj_add_flag(catalog_panel, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(detail_panel, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(keyboard, LV_OBJ_FLAG_HIDDEN);
}

static void show_catalog(lv_event_t *event)
{
    (void)event;
    lv_obj_add_flag(detail_panel, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(catalog_panel, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(keyboard, LV_OBJ_FLAG_HIDDEN);
    catalog_render();
    rt_kprintf("TCM_ATLAS_CATALOG matches=%u page=%u\n", match_count, catalog_page + 1);
}

static lv_obj_t *answer_text(const char *text, uint32_t color)
{
    lv_obj_t *label = atlas_label(answer_scroll, 0, 0, 384, color, "");
    lv_label_set_text_static(label, text);
    lv_obj_set_style_text_line_space(label, 5, 0);
    return label;
}

static lv_obj_t *reading_section(const char *title, const char *text, uint32_t background)
{
    if (!*text) return NULL;
    lv_obj_t *heading = answer_text(title, ATLAS_FOREST);
    lv_obj_set_style_bg_color(heading, lv_color_hex(background), 0);
    lv_obj_set_style_bg_opa(heading, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(heading, 0, 0);
    lv_obj_set_style_radius(heading, 10, 0);
    lv_obj_set_style_pad_all(heading, 10, 0);
    answer_text(text, ATLAS_INK);
    return heading;
}

static void show_entry(unsigned index, unsigned section)
{
    const home_atlas_entry_t *entry = &home_atlas_entries[index];
    lv_label_set_text_fmt(detail_title, "%s  %s", atlas_string(entry->code), atlas_string(entry->name));
    lv_obj_clean(answer_scroll);
    reading_section("笔记", atlas_string(entry->notes), ATLAS_SAGE);
    lv_obj_t *modern = reading_section("现代药理研究", atlas_string(entry->modern), 0xDFE9E7);
    lv_obj_add_flag(catalog_panel, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(detail_panel, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(keyboard, LV_OBJ_FLAG_HIDDEN);
    lv_obj_update_layout(answer_scroll);
    lv_obj_scroll_to_y(answer_scroll, 0, LV_ANIM_OFF);
    if (section && modern) lv_obj_scroll_to_view(modern, LV_ANIM_OFF);
    rt_kprintf("TCM_ATLAS_ENTRY id=%s notes=%u modern=%u section=%u\n", atlas_string(entry->code),
               (unsigned)(*atlas_string(entry->notes) != 0), (unsigned)(*atlas_string(entry->modern) != 0), section);
}

static void entry_clicked(lv_event_t *event)
{
    unsigned row = (unsigned)(uintptr_t)lv_event_get_user_data(event);
    unsigned position = catalog_page * ROWS_PER_PAGE + row;
    if (position < match_count) show_entry(matches[position], 0);
}

static void number_focused(lv_event_t *event)
{
    (void)event;
    lv_keyboard_set_textarea(keyboard, number_input);
    lv_obj_remove_flag(keyboard, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(keyboard);
}

static void number_finished(lv_event_t *event)
{
    (void)event;
    lv_obj_add_flag(keyboard, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_state(number_input, LV_STATE_FOCUSED);
}

static void create_catalog(lv_obj_t *screen)
{
    catalog_panel = atlas_panel(screen);
    atlas_button(catalog_panel, 24, 20, 128, "返回首页", show_home, NULL);
    atlas_label(catalog_panel, 178, 31, 240, ATLAS_INK, "本草图鉴");
    category_selector = lv_dropdown_create(catalog_panel);
    lv_obj_set_pos(category_selector, 24, 91);
    lv_obj_set_size(category_selector, 240, 52);
    style_selector(category_selector);
    char options[1024] = "全部药材";
    size_t used = strlen(options);
    for (unsigned i = 0; i < home_atlas_category_count; ++i) {
        int written = rt_snprintf(options + used, sizeof(options) - used, "\n%s", atlas_string(home_atlas_categories[i]));
        if (written < 0 || (size_t)written >= sizeof(options) - used) break;
        used += (size_t)written;
    }
    lv_dropdown_set_options(category_selector, options);
    number_input = lv_textarea_create(catalog_panel);
    lv_obj_set_pos(number_input, 280, 91);
    lv_obj_set_size(number_input, 176, 52);
    lv_textarea_set_one_line(number_input, true);
    lv_textarea_set_max_length(number_input, 7);
    lv_textarea_set_accepted_chars(number_input, "0123456789.");
    lv_textarea_set_placeholder_text(number_input, "编号 003.1");
    lv_obj_set_style_text_font(number_input, &home_font, 0);
    lv_obj_set_style_bg_color(number_input, lv_color_hex(ATLAS_SURFACE), 0);
    lv_obj_set_style_border_color(number_input, lv_color_hex(ATLAS_BORDER), 0);
    catalog_count = atlas_label(catalog_panel, 26, 151, 430, ATLAS_MUTED, "");
    for (unsigned i = 0; i < ROWS_PER_PAGE; ++i) {
        row_buttons[i] = lv_button_create(catalog_panel);
        lv_obj_set_pos(row_buttons[i], 24, 193 + i * 78);
        lv_obj_set_size(row_buttons[i], 432, 68);
        lv_obj_set_style_pad_all(row_buttons[i], 0, 0);
        lv_obj_set_style_shadow_width(row_buttons[i], 0, 0);
        lv_obj_set_style_bg_color(row_buttons[i], lv_color_hex(i % 2 ? ATLAS_SURFACE : ATLAS_SAGE), 0);
        lv_obj_set_style_radius(row_buttons[i], 14, 0);
        row_names[i] = atlas_label(row_buttons[i], 16, 5, 399, ATLAS_INK, "");
        lv_obj_set_height(row_names[i], 31);
        lv_label_set_long_mode(row_names[i], LV_LABEL_LONG_DOT);
        row_categories[i] = atlas_label(row_buttons[i], 16, 34, 399, ATLAS_MUTED, "");
        lv_obj_add_event_cb(row_buttons[i], entry_clicked, LV_EVENT_CLICKED, (void *)(uintptr_t)i);
    }
    empty_message = atlas_label(catalog_panel, 44, 227, 392, ATLAS_MUTED, "没有匹配的药材。\n清空编号或切换分类再试。");
    previous_button = atlas_button(catalog_panel, 24, 693, 128, "上一页", catalog_turn, (void *)(intptr_t)-1);
    next_button = atlas_button(catalog_panel, 328, 693, 128, "下一页", catalog_turn, (void *)(intptr_t)1);
    page_label = atlas_label(catalog_panel, 160, 704, 160, ATLAS_MUTED, "");
    lv_obj_set_style_text_align(page_label, LV_TEXT_ALIGN_CENTER, 0);
    keyboard = lv_keyboard_create(catalog_panel);
    lv_keyboard_set_mode(keyboard, LV_KEYBOARD_MODE_NUMBER);
    lv_obj_set_size(keyboard, 480, 235);
    lv_obj_align(keyboard, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_set_style_bg_color(keyboard, lv_color_hex(ATLAS_PAPER), 0);
    lv_obj_set_style_bg_color(keyboard, lv_color_hex(ATLAS_SAGE), LV_PART_ITEMS);
    lv_obj_set_style_text_color(keyboard, lv_color_hex(ATLAS_INK), LV_PART_ITEMS);
    lv_obj_add_flag(keyboard, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_event_cb(keyboard, number_finished, LV_EVENT_READY, NULL);
    lv_obj_add_event_cb(keyboard, number_finished, LV_EVENT_CANCEL, NULL);
    lv_obj_add_event_cb(number_input, number_focused, LV_EVENT_FOCUSED, NULL);
    lv_obj_add_event_cb(number_input, number_focused, LV_EVENT_CLICKED, NULL);
    lv_obj_add_event_cb(number_input, catalog_filter, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_add_event_cb(category_selector, catalog_filter, LV_EVENT_VALUE_CHANGED, NULL);
}

static void create_detail(lv_obj_t *screen)
{
    detail_panel = atlas_panel(screen);
    atlas_button(detail_panel, 24, 20, 128, "返回目录", show_catalog, NULL);
    atlas_button(detail_panel, 328, 20, 128, "返回首页", show_home, NULL);
    atlas_label(detail_panel, 184, 31, 124, ATLAS_INK, "药材资料");
    detail_title = atlas_label(detail_panel, 26, 91, 428, ATLAS_INK, "");
    lv_obj_set_height(detail_title, 34);
    lv_label_set_long_mode(detail_title, LV_LABEL_LONG_DOT);
    answer_scroll = lv_obj_create(detail_panel);
    lv_obj_set_pos(answer_scroll, 24, 151);
    lv_obj_set_size(answer_scroll, 432, 625);
    lv_obj_set_style_bg_color(answer_scroll, lv_color_hex(ATLAS_SURFACE), 0);
    lv_obj_set_style_border_width(answer_scroll, 0, 0);
    lv_obj_set_style_radius(answer_scroll, 18, 0);
    lv_obj_set_style_pad_all(answer_scroll, 20, 0);
    lv_obj_set_style_pad_row(answer_scroll, 18, 0);
    lv_obj_set_flex_flow(answer_scroll, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_scroll_dir(answer_scroll, LV_DIR_VER);
}

static void apply_request(lv_timer_t *timer)
{
    (void)timer;
    rt_base_t irq = rt_hw_interrupt_disable();
    int entry = requested_entry;
    unsigned section = requested_section;
    requested_entry = -2;
    rt_hw_interrupt_enable(irq);
    if (entry >= 0) show_entry((unsigned)entry, section);
    else if (entry == -1) show_catalog(NULL);
    else if (entry == -3) show_home(NULL);
}

static int home_atlas_show(int argc, char **argv)
{
    if (argc < 2 || argc > 3 || !matches) return -RT_EINVAL;
    int entry = -2;
    unsigned section = 0;
    if (strcmp(argv[1], "catalog") == 0) entry = -1;
    else if (strcmp(argv[1], "home") == 0) entry = -3;
    else {
        for (unsigned i = 0; i < home_atlas_entry_count; ++i) {
            if (strcmp(argv[1], atlas_string(home_atlas_entries[i].code)) == 0) {
                entry = (int)i;
                break;
            }
        }
    }
    if (argc == 3) {
        char *end;
        unsigned long parsed = strtoul(argv[2], &end, 10);
        if (end == argv[2] || *end || parsed > 1) return -RT_EINVAL;
        section = (unsigned)parsed;
    }
    if (entry == -2) return -RT_EINVAL;
    rt_base_t irq = rt_hw_interrupt_disable();
    requested_section = section;
    requested_entry = entry;
    rt_hw_interrupt_enable(irq);
    return 0;
}
MSH_CMD_EXPORT(home_atlas_show, open an atlas source ID and optional section 0 notes or 1 modern);

void home_atlas_create(void *screen, void *header)
{
    uint32_t crc = UINT32_MAX;
    for (unsigned i = 0; i < home_atlas_blob_size; ++i) {
        crc ^= (unsigned char)home_atlas_blob[i];
        for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320U : 0);
    }
    if (~crc != home_atlas_blob_crc || home_atlas_entry_count > UINT16_MAX) {
        rt_kprintf("TCM_ATLAS_INVALID\n");
        return;
    }
    matches = lv_malloc(home_atlas_entry_count * sizeof(*matches));
    if (!matches) {
        rt_kprintf("TCM_ATLAS_MEMORY_FAILED\n");
        return;
    }
    lv_obj_t *launcher = atlas_button(header, 272, 76, 144, "中药图鉴 >", show_catalog, NULL);
    lv_obj_set_height(launcher, 35);
    lv_obj_set_style_pad_all(launcher, 0, 0);
    create_catalog(screen);
    create_detail(screen);
    catalog_filter(NULL);
    lv_timer_create(apply_request, 100, NULL);
    rt_kprintf("TCM_ATLAS_READY entries=%u bytes=%u crc=%08x\n",
               home_atlas_entry_count, home_atlas_blob_size, home_atlas_blob_crc);
}
#endif
