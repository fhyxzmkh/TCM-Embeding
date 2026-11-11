#include <lvgl.h>
#include <rtthread.h>
#include <rtdevice.h>
#include "home_data.h"
#ifdef TCM_HOME_ATLAS
#include "home_atlas.h"
#endif
#ifdef TCM_HOME_WEATHER
#include "home_weather.h"
#include "home_recommend.h"
#endif

LV_FONT_DECLARE(home_font);
#ifdef TCM_HOME_WEATHER
LV_FONT_DECLARE(home_numbers);
#endif

#define INK 0x273B32
#define FOREST 0x304C40
#define MUTED 0x526559
#define PAPER 0xE8E6DA
#define SURFACE 0xF3F0E5
#define SAGE 0xDEE7D8
#define MINT 0xDCE8D5
#define BORDER 0xCCD4C4
#define ACCENT 0x715539

static lv_obj_t *date_label, *time_label, *term_label, *temperature, *humidity;
static lv_obj_t *environment, *month_label, *calendar, *tip;
static home_date_t shown;
static bool clock_shown, env_shown;
static home_environment_t last_sample;
static bool frame_logged;

static lv_obj_t *card(lv_obj_t *parent, int y, int height, uint32_t color)
{
    lv_obj_t *object = lv_obj_create(parent);
    lv_obj_remove_style_all(object);
    lv_obj_set_pos(object, 24, y);
    lv_obj_set_size(object, 432, height);
    lv_obj_set_style_bg_color(object, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(object, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(object, 18, 0);
    lv_obj_remove_flag(object, LV_OBJ_FLAG_SCROLLABLE);
    return object;
}

static lv_obj_t *label(lv_obj_t *parent, int x, int y, int width,
                       uint32_t color, const lv_font_t *font, const char *initial)
{
    lv_obj_t *object = lv_label_create(parent);
    lv_obj_set_pos(object, x, y);
    lv_obj_set_width(object, width);
    lv_obj_set_style_text_color(object, lv_color_hex(color), 0);
    lv_obj_set_style_text_font(object, font, 0);
    lv_label_set_text(object, initial);
    return object;
}

#ifdef TCM_HOME_WEATHER
static lv_obj_t *weather_panel, *city_selector, *weather_title, *weather_summary, *weather_value;
static lv_obj_t *location_note, *weather_current, *weather_condition, *weather_updated;
static lv_obj_t *forecast_dates[3], *forecast_conditions[3], *forecast_ranges[3];

static const char *weather_state(home_weather_state_t state)
{
    switch (state) {
    case HOME_WEATHER_CONFIG: return "天气待联网配置";
    case HOME_WEATHER_NETWORK: return "正在连接热点";
    case HOME_WEATHER_CLOCK: return "正在网络校时";
    case HOME_WEATHER_CLOCK_ERROR: return "网络校时待重试";
    case HOME_WEATHER_LOADING: return "正在更新天气";
    case HOME_WEATHER_ERROR: return "天气更新失败";
    case HOME_WEATHER_OFFLINE: return "热点连接失败";
    default: return "天气已更新";
    }
}

static const char *location_status(home_location_state_t state)
{
    switch (state) {
    case HOME_LOCATION_MANUAL: return "手动选城";
    case HOME_LOCATION_PENDING: return "联网后自动定位";
    case HOME_LOCATION_IP_API: return "IP-API 估算位置";
    case HOME_LOCATION_IP_SB: return "IP.SB 估算位置";
    case HOME_LOCATION_OUTSIDE: return "网络出口不在大陆 · 使用手选城市";
    case HOME_LOCATION_PROXY: return "检测到代理出口 · 使用手选城市";
    default: return "IP 定位失败 · 使用手选城市";
    }
}

static const char *location_name(const home_weather_t *data)
{
    if (!data->location.coordinates[0]) return home_cities[data->city].name;
    const unsigned char *name = (const unsigned char *)data->location.name;
    if (!*name) return "网络位置";
    for (const unsigned char *p = name; *p;) {
        uint32_t code = *p++;
        if (code >= 0xe0 && code <= 0xef && (p[0] & 0xc0) == 0x80 && (p[1] & 0xc0) == 0x80) {
            code = ((code & 15) << 12) | ((p[0] & 63) << 6) | (p[1] & 63);
            p += 2;
        } else if (code < 32 || code > 126) {
            return "网络位置";
        }
        lv_font_glyph_dsc_t glyph;
        if (!lv_font_get_glyph_dsc(&home_font, &glyph, code, 0) || glyph.is_placeholder) return "网络位置";
    }
    return data->location.name;
}

static void refresh_weather(void)
{
    static unsigned revision = (unsigned)-1;
    home_weather_t data;
    home_weather_get(&data);
    if (data.revision == revision) return;
    revision = data.revision;
    unsigned selected = data.automatic ? 0 : data.city + 1;
    if (lv_dropdown_get_selected(city_selector) != selected)
        lv_dropdown_set_selected(city_selector, selected);
    const char *city = location_name(&data);
    lv_label_set_text_fmt(weather_title, "室外天气  ·  %s", city);
    lv_label_set_text(location_note, location_status(data.location.state));
    const char *state = weather_state(data.state);
    lv_label_set_text_fmt(weather_condition, "%s  ·  %s", city, data.available ? home_weather_condition(data.condition) : state);
    if (!data.available) {
        lv_label_set_text(weather_summary, state);
        lv_label_set_text(weather_value, "-- °C");
        lv_label_set_text(weather_current, "-- °C");
        lv_label_set_text_fmt(weather_updated, "%s\n天气数据  ·  和风天气", state);
        for (unsigned i = 0; i < 3; ++i) {
            lv_label_set_text(forecast_dates[i], "—");
            lv_label_set_text(forecast_conditions[i], "—");
            lv_label_set_text(forecast_ranges[i], "—");
        }
        return;
    }
    const char *condition = home_weather_condition(data.condition);
    bool stale = data.state != HOME_WEATHER_READY && data.state != HOME_WEATHER_LOADING;
    const char *source = data.location.coordinates[0] ? "IP 定位" : data.automatic ? "手选城市" : "手动";
    lv_label_set_text_fmt(weather_summary, "%s  ·  %s", condition, stale ? "上次数据" : source);
    lv_label_set_text_fmt(weather_value, "%d °C", data.temperature);
    lv_label_set_text_fmt(weather_current, "%d °C", data.temperature);
    lv_label_set_text_fmt(weather_updated, "%s  ·  %02u:%02u 更新\n天气数据  ·  和风天气",
                          state, data.updated.hour, data.updated.minute);
    for (unsigned i = 0; i < 3; ++i) {
        home_forecast_t day = data.days[i];
        lv_label_set_text_fmt(forecast_dates[i], "%02u/%02u", day.month, day.day);
        lv_label_set_text(forecast_conditions[i], home_weather_condition(day.condition));
        lv_label_set_text_fmt(forecast_ranges[i], "%d 至 %d °C", day.low, day.high);
    }
}

static void weather_open(lv_event_t *event)
{
    (void)event;
    lv_obj_remove_flag(weather_panel, LV_OBJ_FLAG_HIDDEN);
}

static void weather_close(lv_event_t *event)
{
    (void)event;
    lv_obj_add_flag(weather_panel, LV_OBJ_FLAG_HIDDEN);
}

static void weather_city_changed(lv_event_t *event)
{
    unsigned selection = lv_dropdown_get_selected(lv_event_get_target(event));
    if (selection == 0) home_weather_auto();
    else home_weather_select(selection - 1);
    refresh_weather();
}

static void weather_refresh_clicked(lv_event_t *event)
{
    (void)event;
    home_weather_refresh();
}

static void weather_button(lv_obj_t *parent, int x, int y, const char *name, lv_event_cb_t callback)
{
    lv_obj_t *button = lv_button_create(parent);
    lv_obj_set_pos(button, x, y);
    lv_obj_set_size(button, 128, 52);
    lv_obj_set_style_bg_color(button, lv_color_hex(FOREST), 0);
    lv_obj_set_style_shadow_width(button, 0, 0);
    lv_obj_set_style_radius(button, 14, 0);
    lv_obj_t *text = lv_label_create(button);
    lv_obj_set_style_text_font(text, &home_font, 0);
    lv_obj_set_style_text_color(text, lv_color_hex(SURFACE), 0);
    lv_label_set_text(text, name);
    lv_obj_center(text);
    lv_obj_add_event_cb(button, callback, LV_EVENT_CLICKED, NULL);
}

static void create_weather_card(lv_obj_t *screen)
{
    lv_obj_t *summary = card(screen, 145, 102, SAGE);
    weather_title = label(summary, 20, 10, 278, INK, &home_font, "室外天气");
    lv_obj_set_height(weather_title, 32);
    lv_label_set_long_mode(weather_title, LV_LABEL_LONG_DOT);
    label(summary, 308, 10, 111, MUTED, &home_font, "查看详情 >");
    weather_summary = label(summary, 20, 56, 238, MUTED, &home_font, "");
    weather_value = label(summary, 268, 44, 143, FOREST, &home_numbers, "-- °C");
    lv_obj_set_style_text_align(weather_value, LV_TEXT_ALIGN_RIGHT, 0);
    lv_obj_add_flag(summary, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(summary, weather_open, LV_EVENT_CLICKED, NULL);
}

static void create_weather_panel(lv_obj_t *screen)
{
    weather_panel = lv_obj_create(screen);
    lv_obj_remove_style_all(weather_panel);
    lv_obj_set_size(weather_panel, 480, 800);
    lv_obj_set_style_bg_color(weather_panel, lv_color_hex(PAPER), 0);
    lv_obj_set_style_bg_opa(weather_panel, LV_OPA_COVER, 0);
    lv_obj_remove_flag(weather_panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(weather_panel, LV_OBJ_FLAG_CLICKABLE);
    weather_button(weather_panel, 24, 20, "返回首页", weather_close);
    weather_button(weather_panel, 328, 20, "刷新天气", weather_refresh_clicked);
    lv_obj_t *title = label(weather_panel, 161, 31, 158, INK, &home_font, "城市天气");
    lv_obj_set_style_text_align(title, LV_TEXT_ALIGN_CENTER, 0);

    lv_obj_t *location = card(weather_panel, 91, 154, SURFACE);
    label(location, 20, 10, 392, INK, &home_font, "城市与定位");
    city_selector = lv_dropdown_create(location);
    lv_obj_set_pos(city_selector, 20, 46);
    lv_obj_set_size(city_selector, 392, 52);
    lv_obj_set_style_bg_color(city_selector, lv_color_hex(SAGE), 0);
    lv_obj_set_style_border_color(city_selector, lv_color_hex(BORDER), 0);
    lv_obj_set_style_radius(city_selector, 12, 0);
    lv_obj_set_style_text_color(city_selector, lv_color_hex(INK), 0);
    lv_obj_set_style_text_font(city_selector, &home_font, 0);
    lv_obj_set_style_text_font(city_selector, &lv_font_montserrat_24, LV_PART_INDICATOR);
    lv_obj_t *list = lv_dropdown_get_list(city_selector);
    lv_obj_set_style_text_font(list, &home_font, 0);
    lv_obj_set_style_text_color(list, lv_color_hex(INK), 0);
    lv_obj_set_style_bg_color(list, lv_color_hex(SURFACE), 0);
    lv_obj_set_style_bg_color(list, lv_color_hex(FOREST), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_set_style_text_color(list, lv_color_hex(SURFACE), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_set_style_max_height(list, 425, 0);
    char options[480] = "自动定位";
    size_t length = sizeof("自动定位") - 1;
    for (unsigned i = 0; i < home_city_count; ++i) {
        int written = rt_snprintf(options + length, sizeof(options) - length, "\n%s", home_cities[i].name);
        if (written < 0 || (size_t)written >= sizeof(options) - length) break;
        length += (size_t)written;
    }
    lv_dropdown_set_options(city_selector, options);
    lv_obj_add_event_cb(city_selector, weather_city_changed, LV_EVENT_VALUE_CHANGED, NULL);
    location_note = label(location, 20, 109, 392, MUTED, &home_font, "");

    lv_obj_t *current = card(weather_panel, 259, 120, SAGE);
    label(current, 20, 12, 392, MUTED, &home_font, "室外实况");
    weather_condition = label(current, 20, 62, 255, INK, &home_font, "");
    lv_obj_set_height(weather_condition, 32);
    lv_label_set_long_mode(weather_condition, LV_LABEL_LONG_DOT);
    weather_current = label(current, 275, 47, 136, FOREST, &home_numbers, "");
    lv_obj_set_style_text_align(weather_current, LV_TEXT_ALIGN_RIGHT, 0);

    lv_obj_t *details = card(weather_panel, 393, 254, SURFACE);
    label(details, 20, 13, 392, INK, &home_font, "三天预报");
    for (unsigned i = 0; i < 3; ++i) {
        lv_obj_t *row = lv_obj_create(details);
        lv_obj_remove_style_all(row);
        lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_pos(row, 16, 56 + i * 61);
        lv_obj_set_size(row, 400, 52);
        lv_obj_set_style_bg_color(row, lv_color_hex(SAGE), 0);
        lv_obj_set_style_bg_opa(row, i % 2 ? LV_OPA_TRANSP : LV_OPA_60, 0);
        lv_obj_set_style_radius(row, 11, 0);
        forecast_dates[i] = label(row, 12, 10, 80, MUTED, &home_font, "—");
        forecast_conditions[i] = label(row, 104, 10, 125, INK, &home_font, "—");
        forecast_ranges[i] = label(row, 228, 10, 156, INK, &home_font, "—");
        lv_obj_set_style_text_align(forecast_ranges[i], LV_TEXT_ALIGN_RIGHT, 0);
    }
    weather_updated = label(weather_panel, 44, 676, 392, MUTED, &home_font, "");
    lv_obj_add_flag(weather_panel, LV_OBJ_FLAG_HIDDEN);
}
#endif

static void refresh(lv_timer_t *timer)
{
    (void)timer;
#ifdef TCM_HOME_WEATHER
    refresh_weather();
#endif
    static const char *const weekday[] = {"日", "一", "二", "三", "四", "五", "六"};
    home_date_t now;
    if (home_clock_get(&now)) {
        if (!clock_shown || now.hour != shown.hour || now.minute != shown.minute)
            lv_label_set_text_fmt(time_label, "%02u:%02u", now.hour, now.minute);
        if (!clock_shown || now.year != shown.year || now.month != shown.month || now.day != shown.day) {
            bool on_term;
            const int term = home_solar_term(now, &on_term);
            lv_label_set_text_fmt(date_label, "%u年%u月%u日  星期%s",
                                  now.year, now.month, now.day, weekday[home_weekday(now)]);
            if (term >= 0)
                lv_label_set_text_fmt(term_label, "%s  ·  %s",
                                      on_term ? "今日节气" : "当前节气", home_term_name((unsigned)term));
            else
                lv_label_set_text(term_label, "节气资料未覆盖");
            lv_label_set_text_fmt(month_label, "%u月日历", now.month);
            lv_calendar_set_today_date(calendar, now.year, now.month, now.day);
            lv_calendar_set_showed_date(calendar, now.year, now.month);
            lv_obj_remove_flag(calendar, LV_OBJ_FLAG_HIDDEN);
#ifndef TCM_HOME_WEATHER
            lv_label_set_text(tip, home_daily_tip(now, term));
#endif
        }
        shown = now;
        clock_shown = true;
    } else if (clock_shown) {
        clock_shown = false;
        lv_label_set_text(date_label, "日期待校时");
        lv_label_set_text(time_label, "--:--");
        lv_label_set_text(term_label, "校时后显示节气");
        lv_label_set_text(month_label, "月历待校时");
        lv_obj_add_flag(calendar, LV_OBJ_FLAG_HIDDEN);
#ifndef TCM_HOME_WEATHER
        lv_label_set_text(tip, home_daily_tip(now, -1));
#endif
    }

#ifdef TCM_HOME_WEATHER
    home_recommend_refresh();
#endif
    home_environment_t sample;
    if (home_environment_get(&sample)) {
        if (!env_shown || sample.temperature_tenths != last_sample.temperature_tenths ||
            sample.humidity_tenths != last_sample.humidity_tenths) {
            const int t = sample.temperature_tenths;
            const unsigned magnitude = (unsigned)(t < 0 ? -t : t);
            lv_label_set_text_fmt(temperature, "%s%u.%u °C", t < 0 ? "-" : "", magnitude / 10, magnitude % 10);
            lv_label_set_text_fmt(humidity, "%u.%u %%", sample.humidity_tenths / 10, sample.humidity_tenths % 10);
            lv_label_set_text(environment, home_environment_tip(sample));
            last_sample = sample;
        }
        env_shown = true;
    } else if (env_shown) {
        env_shown = false;
        lv_label_set_text(temperature, "-- °C");
        lv_label_set_text(humidity, "-- %");
        lv_label_set_text(environment, "暂未取得温湿度数据");
    }
}

static void first_frame(lv_event_t *event)
{
    (void)event;
    if (!frame_logged) {
        frame_logged = true;
        rt_kprintf("TCM_HOME_FIRST_FRAME\n");
    }
}

void lv_user_gui_init(void)
{
    lv_display_t *display = lv_display_get_default();
#if LV_USE_THEME_DEFAULT
    lv_display_set_theme(display, lv_theme_default_init(display, lv_color_hex(FOREST),
                         lv_color_hex(ACCENT), false, LV_FONT_DEFAULT));
#endif
#ifdef RT_USING_PWM
    /* Edgi-Talk LCD uses pwm18 channel 0 and a 200 us period in drv_lcd.c. */
    struct rt_device_pwm *backlight = (struct rt_device_pwm *)rt_device_find("pwm18");
    if (backlight && rt_pwm_set(backlight, 0, 200000, 130000) == RT_EOK)
        rt_kprintf("TCM_HOME_BACKLIGHT percent=65\n");
#endif
    lv_obj_t *screen = lv_screen_active();
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(screen, lv_color_hex(PAPER), 0);

#ifdef TCM_HOME_WEATHER
    const int header_y = 20, header_height = 113, term_y = 77, environment_y = 83;
    const lv_font_t *clock_font = &home_numbers;
#else
    const int header_y = 24, header_height = 126, term_y = 84, environment_y = 99;
    const lv_font_t *clock_font = &lv_font_montserrat_24;
#endif
    lv_obj_t *header = card(screen, header_y, header_height, FOREST);
    label(header, 20, 11, 240, SURFACE, &home_font, "四时小记");
    time_label = label(header, 279, 12, 132, SURFACE, clock_font, "--:--");
    lv_obj_set_style_text_align(time_label, LV_TEXT_ALIGN_RIGHT, 0);
    date_label = label(header, 20, 48, 392, SURFACE, &home_font, "日期待校时");
    term_label = label(header, 20, term_y, 392, MINT, &home_font, "校时后显示节气");

#ifdef TCM_HOME_WEATHER
    create_weather_card(screen);
    lv_obj_t *env = card(screen, 259, 122, SURFACE);
#else
    lv_obj_t *env = card(screen, 166, 146, SURFACE);
#endif
    label(env, 20, 11, 170, MUTED, &home_font, "室内温度");
    label(env, 225, 11, 185, MUTED, &home_font, "相对湿度");
    temperature = label(env, 20, 44, 190, INK, &lv_font_montserrat_24, "-- °C");
    humidity = label(env, 225, 44, 185, INK, &lv_font_montserrat_24, "-- %");
    environment = label(env, 20, environment_y, 392, MUTED, &home_font, "暂未取得温湿度数据");

#ifdef TCM_HOME_WEATHER
    lv_obj_t *month = card(screen, 393, 262, SURFACE);
#else
    lv_obj_t *month = card(screen, 328, 268, SURFACE);
#endif
    month_label = label(month, 20, 8, 392, INK, &home_font, "月历待校时");
    calendar = lv_calendar_create(month);
    lv_obj_set_pos(calendar, 16, 44);
    lv_obj_set_size(calendar, 400, 211);
    lv_obj_set_style_bg_opa(calendar, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(calendar, 0, 0);
    lv_obj_set_style_pad_all(calendar, 0, 0);
    lv_obj_t *buttons = lv_calendar_get_btnmatrix(calendar);
    lv_obj_set_style_text_font(buttons, &home_font, LV_PART_ITEMS);
    lv_obj_set_style_text_color(buttons, lv_color_hex(INK), LV_PART_ITEMS);
    lv_obj_set_style_bg_color(buttons, lv_color_hex(SAGE), LV_PART_ITEMS);
    lv_obj_set_style_bg_opa(buttons, LV_OPA_50, LV_PART_ITEMS);
    lv_obj_set_style_border_color(buttons, lv_color_hex(BORDER), LV_PART_ITEMS);
    lv_obj_set_style_border_opa(buttons, LV_OPA_60, LV_PART_ITEMS);
    lv_obj_set_style_radius(buttons, 6, LV_PART_ITEMS);
    lv_obj_remove_flag(buttons, LV_OBJ_FLAG_CLICKABLE);
    static const char *day_names[] = {
#if LV_CALENDAR_WEEK_STARTS_MONDAY
        "一", "二", "三", "四", "五", "六", "日"
#else
        "日", "一", "二", "三", "四", "五", "六"
#endif
    };
    lv_calendar_set_day_names(calendar, day_names);
    lv_obj_add_flag(calendar, LV_OBJ_FLAG_HIDDEN);

#ifdef TCM_HOME_WEATHER
    lv_obj_t *daily = card(screen, 667, 117, SURFACE);
#else
    lv_obj_t *daily = card(screen, 612, 150, SURFACE);
#endif
    label(daily, 20, 12, 392, ACCENT, &home_font, "今日推荐");
    tip = label(daily, 20, 51, 392, INK, &home_font, "校时后显示当天的节气和推荐。");
    lv_obj_set_style_text_line_space(tip, 4, 0);
#ifdef TCM_HOME_WEATHER
    create_weather_panel(screen);
    home_recommend_create(screen, daily, tip);
#endif
#ifdef TCM_HOME_ATLAS
    home_atlas_create(screen, header);
#endif

#if LV_USE_PERF_MONITOR
    lv_sysmon_hide_performance(display);
#endif
    lv_display_add_event_cb(display, first_frame, LV_EVENT_REFR_READY, NULL);
    refresh(NULL);
    lv_timer_create(refresh, 1000, NULL);
    rt_kprintf("TCM_HOME_GUI width=%d height=%d\n",
               (int)lv_display_get_horizontal_resolution(display), (int)lv_display_get_vertical_resolution(display));
}
