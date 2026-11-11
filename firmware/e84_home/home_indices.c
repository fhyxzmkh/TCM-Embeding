#include <rtconfig.h>
#ifdef TCM_HOME_WEATHER
#include <cJSON.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "home_weather.h"

bool home_indices_usable(const home_indices_t *value, home_date_t date, const char *coordinates)
{
    return home_date_valid(date) && value->count > 0 && value->count <= HOME_INDEX_COUNT &&
           value->date.year == date.year && value->date.month == date.month && value->date.day == date.day &&
           strcmp(value->coordinates, coordinates) == 0;
}

static bool copy_text(const cJSON *field, char *output, size_t capacity, bool optional)
{
    if (!field || cJSON_IsNull(field)) return optional;
    if (!cJSON_IsString(field) || strlen(field->valuestring) >= capacity) return false;
    const unsigned char *p = (const unsigned char *)field->valuestring;
    while (*p) {
        unsigned c = *p++, count;
        if (c < 0x80) {
            if (c < 32 && c != '\n' && c != '\t') return false;
            continue;
        }
        if (c >= 0xc2 && c <= 0xdf) { count = 1; c &= 31; }
        else if (c >= 0xe0 && c <= 0xef) { count = 2; c &= 15; }
        else if (c >= 0xf0 && c <= 0xf4) { count = 3; c &= 7; }
        else return false;
        for (unsigned i = 0; i < count; ++i) {
            if ((*p & 0xc0) != 0x80) return false;
            c = (c << 6) | (*p++ & 63);
        }
        if (c < (count == 1 ? 0x80U : count == 2 ? 0x800U : 0x10000U) ||
            (c >= 0xd800 && c <= 0xdfff) || c > 0x10ffff) return false;
    }
    strcpy(output, field->valuestring);
    return true;
}

int home_indices_parse(const cJSON *json, home_date_t date, home_indices_t *value)
{
    if (!home_date_valid(date)) return -105;
    const cJSON *code = cJSON_GetObjectItemCaseSensitive(json, "code");
    const cJSON *daily = cJSON_GetObjectItemCaseSensitive(json, "daily");
    if (!cJSON_IsString(code) || strcmp(code->valuestring, "200") || !cJSON_IsArray(daily) ||
        cJSON_GetArraySize(daily) > 16) return -105;
    char expected[16];
    snprintf(expected, sizeof(expected), "%04u-%02u-%02u", date.year, date.month, date.day);
    memset(value, 0, sizeof(*value));
    value->date = (home_date_t){date.year, date.month, date.day, 0, 0, 0};
    static const char *const types[] = {"1", "3", "8"};
    for (unsigned i = 0; i < HOME_INDEX_COUNT; ++i) {
        const cJSON *item;
        cJSON_ArrayForEach(item, daily) {
            const cJSON *type = cJSON_GetObjectItemCaseSensitive(item, "type");
            const cJSON *day = cJSON_GetObjectItemCaseSensitive(item, "date");
            const cJSON *level = cJSON_GetObjectItemCaseSensitive(item, "level");
            if (!cJSON_IsString(type) || strcmp(type->valuestring, types[i]) ||
                !cJSON_IsString(day) || strcmp(day->valuestring, expected) ||
                !cJSON_IsString(level) || strlen(level->valuestring) != 1 ||
                level->valuestring[0] < '1' || level->valuestring[0] > (i == 0 ? '3' : '7')) continue;
            home_index_t *out = &value->items[value->count];
            memset(out, 0, sizeof(*out));
            if (!copy_text(cJSON_GetObjectItemCaseSensitive(item, "category"), out->category, sizeof(out->category), false) ||
                !out->category[0] || !copy_text(cJSON_GetObjectItemCaseSensitive(item, "text"), out->text, sizeof(out->text), true))
                continue;
            out->type = (unsigned)strtoul(types[i], NULL, 10);
            out->level = (unsigned)(level->valuestring[0] - '0');
            ++value->count;
            break;
        }
    }
    return value->count ? 0 : -105;
}
#endif
