#ifndef TCM_HOME_WEATHER_H
#define TCM_HOME_WEATHER_H

#include <stdbool.h>
#include <stddef.h>
#include "home_data.h"

typedef struct {
    const char *name;
    const char *coordinates;
} home_city_t;

typedef struct {
    unsigned month, day, condition;
    int low, high;
} home_forecast_t;

typedef enum {
    HOME_WEATHER_CONFIG, HOME_WEATHER_NETWORK, HOME_WEATHER_CLOCK,
    HOME_WEATHER_LOADING, HOME_WEATHER_READY, HOME_WEATHER_ERROR, HOME_WEATHER_OFFLINE,
    HOME_WEATHER_CLOCK_ERROR
} home_weather_state_t;

typedef enum {
    HOME_LOCATION_MANUAL, HOME_LOCATION_PENDING, HOME_LOCATION_IP_API,
    HOME_LOCATION_IP_SB, HOME_LOCATION_UNAVAILABLE, HOME_LOCATION_OUTSIDE, HOME_LOCATION_PROXY
} home_location_state_t;

typedef struct {
    home_location_state_t state;
    char name[64];
    char coordinates[20];
} home_location_t;

typedef struct {
    unsigned city, revision, condition;
    int temperature;
    bool available, automatic;
    home_weather_state_t state;
    home_location_t location;
    home_date_t updated;
    home_forecast_t days[3];
} home_weather_t;

#define HOME_INDEX_COUNT 3
#define HOME_INDEX_TEXT_SIZE 1024
typedef struct {
    unsigned type, level;
    char category[64];
    char text[HOME_INDEX_TEXT_SIZE];
} home_index_t;

typedef struct {
    home_date_t date;
    char coordinates[20];
    unsigned count, revision;
    home_index_t items[HOME_INDEX_COUNT];
} home_indices_t;

extern const home_city_t home_cities[];
extern const unsigned home_city_count;

void home_weather_start(void);
void home_weather_select(unsigned city);
void home_weather_auto(void);
void home_weather_refresh(void);
void home_weather_get(home_weather_t *value);
void home_indices_get(home_indices_t *value);
bool home_indices_usable(const home_indices_t *value, home_date_t date, const char *coordinates);
struct cJSON;
int home_indices_parse(const struct cJSON *json, home_date_t date, home_indices_t *value);
int home_indices_request(const char *host, const char *key, const char *coordinates,
                         home_date_t date, home_indices_t *value);
const char *home_weather_condition(unsigned code);
int home_weather_request(const char *host, const char *key, const char *coordinates, home_weather_t *value);
void home_location_request(home_location_t *location);
bool home_network_time(home_date_t *date);
int home_wifi_connect(const char *ssid, const char *password);
bool home_wifi_ready(void);

#endif
