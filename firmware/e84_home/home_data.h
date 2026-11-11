#ifndef TCM_HOME_DATA_H
#define TCM_HOME_DATA_H

#include <stdbool.h>
#include <stdint.h>

typedef struct { unsigned year, month, day, hour, minute, second; } home_date_t;
typedef struct { int temperature_tenths; unsigned humidity_tenths; } home_environment_t;

bool home_date_valid(home_date_t date);
unsigned home_weekday(home_date_t date);
int home_solar_term(home_date_t date, bool *is_today);
const char *home_term_name(unsigned index);
const char *home_daily_tip(home_date_t date, int term);
const char *home_environment_tip(home_environment_t sample);
bool home_decode_aht20(const uint8_t bytes[7], home_environment_t *result);

bool home_clock_get(home_date_t *date);
int home_clock_set(home_date_t date);
bool home_environment_get(home_environment_t *sample);
int home_services_start(void);

#endif
