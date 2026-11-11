#include "home_data.h"
#include "home_term_days.h"

static const char *const terms[] = {
    "小寒", "大寒", "立春", "雨水", "惊蛰", "春分", "清明", "谷雨",
    "立夏", "小满", "芒种", "夏至", "小暑", "大暑", "立秋", "处暑",
    "白露", "秋分", "寒露", "霜降", "立冬", "小雪", "大雪", "冬至"
};

static const char *const tips[4][4] = {
    {"午后散一会儿步，留意沿途新长的草木。", "早晚添减衣物，出门前看看当天的天气。",
     "整理窗边的小角落，给绿植留一点光。", "读一页本草笔记，记下今天见到的一株植物。"},
    {"把水杯放在手边，忙起来也记得喝水。", "安排户外活动时，尽量避开午后最热的时候。",
     "走过树荫时慢一点，留几分钟看看周围。", "久坐一段时间后，起身走一走。"},
    {"早晚温差较大时，出门带一件薄外套。", "趁天晴整理一下窗边，留意室内湿度。",
     "在白天留一段散步时间，看看树叶的变化。", "给今天记一句小事，睡前留一点安静时间。"},
    {"日落比夏天早，散步可以安排在白天。", "出门前看一下气温，按需要多带一层衣物。",
     "找一段安静时间，读几页喜欢的书。", "把房间收拾出一小块空地，起身活动几分钟。"}
};

static unsigned days_in_month(unsigned year, unsigned month)
{
    static const unsigned days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    const bool leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    return days[month - 1] + (month == 2 && leap);
}

bool home_date_valid(home_date_t d)
{
    return d.year >= 2025 && d.year <= 2099 && d.month >= 1 && d.month <= 12 &&
           d.day >= 1 && d.day <= days_in_month(d.year, d.month) &&
           d.hour < 24 && d.minute < 60 && d.second < 60;
}

unsigned home_weekday(home_date_t d)
{
    static const unsigned offsets[] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
    unsigned y = d.year - (d.month < 3);
    return (y + y / 4 - y / 100 + y / 400 + offsets[d.month - 1] + d.day) % 7;
}

int home_solar_term(home_date_t d, bool *is_today)
{
    *is_today = false;
    if (!home_date_valid(d) || d.year < HOME_TERM_FIRST_YEAR || d.year > HOME_TERM_LAST_YEAR) return -1;
    const uint8_t *row = home_term_days[d.year - HOME_TERM_FIRST_YEAR];
    const int i = (int)(d.month - 1) * 2;
    if (d.day >= row[i + 1]) { *is_today = d.day == row[i + 1]; return i + 1; }
    if (d.day >= row[i]) { *is_today = d.day == row[i]; return i; }
    return (i + 23) % 24;
}

const char *home_term_name(unsigned index) { return index < 24 ? terms[index] : ""; }

const char *home_daily_tip(home_date_t d, int term)
{
    if (!home_date_valid(d)) return "校时后显示当天的节气和推荐。";
    const unsigned season = term < 0 ? ((d.month + 9) % 12) / 3 : ((unsigned)(term + 22) % 24) / 6;
    unsigned day_of_year = d.day;
    for (unsigned month = 1; month < d.month; ++month) day_of_year += days_in_month(d.year, month);
    return tips[season][day_of_year % 4];
}

const char *home_environment_tip(home_environment_t s)
{
    if (s.humidity_tenths >= 600) return "室内偏湿，留意积水和结露。";
    if (s.humidity_tenths < 300) return "室内偏干，可以留意湿度变化。";
    if (s.temperature_tenths >= 280) return "室内偏暖，适时调整室温。";
    if (s.temperature_tenths < 180) return "室内偏凉，按需要增添衣物。";
    return "当前温湿度未触发提醒。";
}

bool home_decode_aht20(const uint8_t b[7], home_environment_t *result)
{
    if ((b[0] & 0x88) != 0x08) return false;
    uint8_t crc = 0xff;
    for (unsigned i = 0; i < 6; ++i) {
        crc ^= b[i];
        for (unsigned j = 0; j < 8; ++j) crc = (uint8_t)((crc << 1) ^ ((crc & 0x80) ? 0x31 : 0));
    }
    if (crc != b[6]) return false;
    uint32_t rh = ((uint32_t)b[1] << 12) | ((uint32_t)b[2] << 4) | (b[3] >> 4);
    uint32_t t = ((uint32_t)(b[3] & 0x0f) << 16) | ((uint32_t)b[4] << 8) | b[5];
    result->humidity_tenths = (rh * 1000U + (1U << 19)) >> 20;
    result->temperature_tenths = (int)((t * 2000U + (1U << 19)) >> 20) - 500;
    return true;
}
