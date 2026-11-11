#include <rtconfig.h>
#ifdef TCM_HOME_WEATHER
#include <rtthread.h>
#include <rthw.h>
#include <finsh.h>
#include <stdlib.h>
#include <string.h>
#include "home_weather.h"
#include "home_weather_defaults.h"

const home_city_t home_cities[] = {
    {"北京", "39.90/116.41"}, {"上海", "31.23/121.47"}, {"广州", "23.13/113.26"},
    {"深圳", "22.54/114.06"}, {"杭州", "30.27/120.15"}, {"南京", "32.06/118.80"},
    {"武汉", "30.59/114.31"}, {"成都", "30.67/104.07"}, {"重庆", "29.56/106.55"},
    {"西安", "34.34/108.94"}, {"郑州", "34.75/113.63"}, {"长沙", "28.23/112.94"},
    {"济南", "36.65/117.12"}, {"福州", "26.08/119.30"}, {"厦门", "24.48/118.09"},
    {"合肥", "31.82/117.23"}, {"南昌", "28.68/115.86"}, {"天津", "39.08/117.20"},
    {"沈阳", "41.81/123.43"}, {"哈尔滨", "45.80/126.53"}, {"昆明", "25.04/102.71"},
    {"贵阳", "26.65/106.63"}, {"兰州", "36.06/103.83"}, {"乌鲁木齐", "43.83/87.62"},
    {"南宁", "22.82/108.32"}, {"海口", "20.04/110.20"}, {"石家庄", "38.04/114.51"},
    {"太原", "37.87/112.55"}, {"呼和浩特", "40.84/111.75"}, {"长春", "43.82/125.32"},
    {"西宁", "36.62/101.78"}, {"银川", "38.49/106.23"}, {"拉萨", "29.65/91.17"},
};
const unsigned home_city_count = sizeof(home_cities) / sizeof(home_cities[0]);

static struct rt_mutex weather_lock;
static struct rt_semaphore wake_worker;
static home_weather_t weather = {.automatic = true, .location.state = HOME_LOCATION_PENDING};
static home_indices_t indices;
static unsigned generation;
static char api_host[81] = HOME_DEFAULT_QWEATHER_HOST;
static char api_key[33] = HOME_DEFAULT_QWEATHER_KEY;
static char wifi_ssid[33] = HOME_DEFAULT_WIFI_SSID;
static char wifi_password[33] = HOME_DEFAULT_WIFI_PASSWORD;
static unsigned wifi_generation, location_generation, indices_generation;

const char *home_weather_condition(unsigned code)
{
    if (code == 100 || code == 150) return "晴";
    if ((code >= 101 && code <= 103) || (code >= 151 && code <= 153)) return "多云";
    if (code == 104) return "阴";
    if (code >= 300 && code < 400) return code == 302 || code == 303 || code == 304 ? "雷阵雨" : "有雨";
    if (code == 404 || code == 405 || code == 406 || code == 456) return "雨夹雪";
    if (code >= 400 && code < 500) return "有雪";
    if (code == 500 || code == 501 || code == 509 || code == 510 || code == 514 || code == 515) return "有雾";
    if (code == 502 || (code >= 511 && code <= 513)) return "霾";
    if (code >= 503 && code <= 508) return "沙尘";
    return "天气待确认";
}

void home_weather_get(home_weather_t *value)
{
    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
    *value = weather;
    rt_mutex_release(&weather_lock);
}

void home_indices_get(home_indices_t *value)
{
    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
    *value = indices;
    rt_mutex_release(&weather_lock);
}

static void request_update(void)
{
    ++generation;
    rt_sem_release(&wake_worker);
}

void home_weather_select(unsigned city)
{
    if (city >= home_city_count) return;
    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
    if (weather.automatic || weather.city != city) {
        weather.city = city;
        weather.automatic = false;
        weather.location = (home_location_t){.state = HOME_LOCATION_MANUAL};
        weather.available = false;
        weather.state = HOME_WEATHER_LOADING;
        ++weather.revision;
        request_update();
    }
    rt_mutex_release(&weather_lock);
}

void home_weather_auto(void)
{
    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
    if (!weather.automatic) {
        weather.automatic = true;
        weather.location = (home_location_t){.state = HOME_LOCATION_PENDING};
        ++weather.revision;
        ++location_generation;
        request_update();
    }
    rt_mutex_release(&weather_lock);
}

void home_weather_refresh(void)
{
    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
    if (weather.automatic) ++location_generation;
    ++indices_generation;
    request_update();
    rt_mutex_release(&weather_lock);
}

static void set_state(unsigned id, home_weather_state_t state)
{
    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
    if (generation == id && weather.state != state) {
        weather.state = state;
        ++weather.revision;
    }
    rt_mutex_release(&weather_lock);
}

static void weather_worker(void *parameter)
{
    (void)parameter;
    unsigned connected_config = 0, resolved_location = (unsigned)-1;
    unsigned scheduled_generation = (unsigned)-1, fetched_indices_generation = (unsigned)-1;
    rt_tick_t next_weather = 0, next_indices = 0;
    while (true) {
        while (rt_sem_trytake(&wake_worker) == RT_EOK) {}
        home_weather_t result;
        home_indices_t cached_indices;
        char host[sizeof(api_host)], key[sizeof(api_key)];
        char ssid[sizeof(wifi_ssid)], password[sizeof(wifi_password)];
        rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
        result = weather;
        cached_indices = indices;
        unsigned id = generation, wifi_id = wifi_generation, location_id = location_generation;
        unsigned indices_id = indices_generation;
        memcpy(host, api_host, sizeof(host));
        memcpy(key, api_key, sizeof(key));
        memcpy(ssid, wifi_ssid, sizeof(ssid));
        memcpy(password, wifi_password, sizeof(password));
        rt_mutex_release(&weather_lock);
        if (scheduled_generation != id) {
            next_weather = next_indices = rt_tick_get();
            scheduled_generation = id;
        }

        unsigned delay = 30000;
        if (!host[0] || !key[0] || !ssid[0]) {
            set_state(id, HOME_WEATHER_CONFIG);
        } else {
            if (connected_config != wifi_id || !home_wifi_ready()) {
                set_state(id, HOME_WEATHER_NETWORK);
                if (home_wifi_connect(ssid, password) == 0) {
                    connected_config = wifi_id;
                    resolved_location = (unsigned)-1;
                    next_weather = next_indices = rt_tick_get();
                } else {
                    set_state(id, HOME_WEATHER_OFFLINE);
                }
            }
            bool online = connected_config == wifi_id && home_wifi_ready();
            bool clock_ready = online && home_clock_get(&result.updated);
            if (online && !clock_ready) {
                set_state(id, HOME_WEATHER_CLOCK);
                home_date_t synchronized;
                bool obtained = home_network_time(&synchronized);
                clock_ready = home_clock_get(&result.updated) ||
                              (obtained && home_clock_set(synchronized) == RT_EOK);
                if (!clock_ready) set_state(id, HOME_WEATHER_CLOCK_ERROR);
            }
            if (clock_ready) {
                if (result.automatic && resolved_location != location_id) {
                    set_state(id, HOME_WEATHER_LOADING);
                    home_location_t location;
                    home_location_request(&location);
                    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
                    bool current = id == generation;
                    if (current) {
                        if (strcmp(weather.location.coordinates, location.coordinates) != 0)
                            weather.available = false;
                        weather.location = location;
                        ++weather.revision;
                        result = weather;
                        resolved_location = location_id;
                    }
                    rt_mutex_release(&weather_lock);
                    if (!current) {
                        memset(password, 0, sizeof(password));
                        memset(key, 0, sizeof(key));
                        continue;
                    }
                }
                const char *coordinates = result.location.coordinates[0] ?
                    result.location.coordinates : home_cities[result.city].coordinates;
                if ((rt_int32_t)(rt_tick_get() - next_weather) >= 0) {
                    set_state(id, HOME_WEATHER_LOADING);
                    int error = home_weather_request(host, key, coordinates, &result);
                    next_weather = rt_tick_get() + rt_tick_from_millisecond(error ? 30000 : 20 * 60 * 1000);
                    if (error == 0) {
                        home_clock_get(&result.updated);
                        rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
                        bool current = id == generation;
                        if (current) {
                            result.available = true;
                            result.state = HOME_WEATHER_READY;
                            result.revision = weather.revision + 1;
                            weather = result;
                        }
                        rt_mutex_release(&weather_lock);
                        if (current) rt_kprintf("TCM_WEATHER_READY city=%u condition=%u temperature=%d location=%u days=%u/%u,%u/%u,%u/%u\n",
                                               result.city, result.condition, result.temperature, (unsigned)result.location.state,
                                               result.days[0].month, result.days[0].day,
                                               result.days[1].month, result.days[1].day,
                                               result.days[2].month, result.days[2].day);
                    } else {
                        rt_kprintf("TCM_WEATHER_ERROR code=%d\n", error);
                        set_state(id, HOME_WEATHER_ERROR);
                    }
                }

                home_date_t today;
                bool need_indices = !home_clock_get(&today) ||
                    !home_indices_usable(&cached_indices, today, coordinates) || fetched_indices_generation != indices_id;
                if (need_indices && (rt_int32_t)(rt_tick_get() - next_indices) >= 0) {
                    home_indices_t received;
                    int error = home_indices_request(host, key, coordinates, today, &received);
                    next_indices = rt_tick_get() + rt_tick_from_millisecond(60000);
                    if (!error) {
                        rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
                        bool current = id == generation;
                        if (current) {
                            received.revision = indices.revision + 1;
                            indices = received;
                            fetched_indices_generation = indices_id;
                        }
                        rt_mutex_release(&weather_lock);
                        if (current) rt_kprintf("TCM_INDICES_READY count=%u date=%04u-%02u-%02u coordinates=%s\n",
                            received.count, received.date.year, received.date.month, received.date.day, coordinates);
                        need_indices = !current;
                    } else rt_kprintf("TCM_INDICES_ERROR code=%d\n", error);
                }
                rt_tick_t now_tick = rt_tick_get();
                rt_int32_t weather_wait = (rt_int32_t)(next_weather - now_tick);
                rt_int32_t indices_wait = (rt_int32_t)(next_indices - now_tick);
                delay = weather_wait <= 0 ? 1000 : (unsigned)((uint64_t)weather_wait * 1000 / RT_TICK_PER_SECOND);
                if (delay > 60000) delay = 60000;
                if (need_indices && indices_wait < weather_wait) {
                    unsigned retry = indices_wait <= 0 ? 1000 : (unsigned)((uint64_t)indices_wait * 1000 / RT_TICK_PER_SECOND);
                    if (retry < delay) delay = retry;
                }
            }
        }
        memset(password, 0, sizeof(password));
        memset(key, 0, sizeof(key));
        rt_sem_take(&wake_worker, rt_tick_from_millisecond(delay));
    }
}

void home_weather_start(void)
{
    rt_mutex_init(&weather_lock, "weather", RT_IPC_FLAG_PRIO);
    rt_sem_init(&wake_worker, "weather", 0, RT_IPC_FLAG_FIFO);
    rt_thread_t thread = rt_thread_create("weather", weather_worker, NULL, 24576, 24, 10);
    if (thread) rt_thread_startup(thread);
}

static int home_weather_wifi(int argc, char **argv)
{
    if (argc != 3 || !argv[1][0] || strlen(argv[1]) >= sizeof(wifi_ssid) ||
        strlen(argv[2]) < 8 || strlen(argv[2]) >= sizeof(wifi_password)) return -RT_EINVAL;
    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
    memset(wifi_password, 0, sizeof(wifi_password));
    strcpy(wifi_ssid, argv[1]);
    strcpy(wifi_password, argv[2]);
    ++wifi_generation;
    request_update();
    rt_mutex_release(&weather_lock);
    rt_kprintf("TCM_WEATHER_WIFI_CONFIGURED\n");
    return 0;
}
MSH_CMD_EXPORT(home_weather_wifi, set a 2.4 GHz hotspot for this session);

static int home_weather_api(int argc, char **argv)
{
    const char suffix[] = ".qweatherapi.com";
    if (argc != 3 || strlen(argv[1]) >= sizeof(api_host) ||
        strlen(argv[1]) <= sizeof(suffix) - 1 || strlen(argv[2]) != sizeof(api_key) - 1 ||
        strcmp(argv[1] + strlen(argv[1]) - (sizeof(suffix) - 1), suffix) != 0 ||
        strspn(argv[1], "abcdefghijklmnopqrstuvwxyz0123456789.-") != strlen(argv[1]) ||
        strspn(argv[2], "0123456789abcdefABCDEF") != strlen(argv[2])) return -RT_EINVAL;
    rt_mutex_take(&weather_lock, RT_WAITING_FOREVER);
    strcpy(api_host, argv[1]);
    strcpy(api_key, argv[2]);
    request_update();
    rt_mutex_release(&weather_lock);
    rt_kprintf("TCM_WEATHER_API_CONFIGURED\n");
    return 0;
}
MSH_CMD_EXPORT(home_weather_api, set QWeather host and key for this session);

static int home_weather_city(int argc, char **argv)
{
    if (argc != 2) return -RT_EINVAL;
    if (strcmp(argv[1], "auto") == 0) {
        home_weather_auto();
        home_weather_refresh();
        return 0;
    }
    char *end;
    unsigned long city = strtoul(argv[1], &end, 10);
    if (end == argv[1] || *end || city >= home_city_count) return -RT_EINVAL;
    home_weather_select((unsigned)city);
    return 0;
}
MSH_CMD_EXPORT(home_weather_city, select the homepage city index);

static int home_indices_status(void)
{
    static home_indices_t snapshot;
    home_weather_t current;
    home_date_t today;
    home_indices_get(&snapshot);
    home_weather_get(&current);
    home_clock_get(&today);
    const char *coordinates = current.location.coordinates[0] ? current.location.coordinates : home_cities[current.city].coordinates;
    rt_kprintf("TCM_INDICES_STATUS usable=%u count=%u date=%04u-%02u-%02u coordinates=%s revision=%u\n",
        (unsigned)home_indices_usable(&snapshot, today, coordinates), snapshot.count,
        snapshot.date.year, snapshot.date.month, snapshot.date.day, snapshot.coordinates, snapshot.revision);
    for (unsigned i = 0; i < snapshot.count; ++i)
        rt_kprintf("TCM_INDEX type=%u level=%u category=%s text=%s\n", snapshot.items[i].type,
                   snapshot.items[i].level, snapshot.items[i].category, snapshot.items[i].text);
    return 0;
}
MSH_CMD_EXPORT(home_indices_status, inspect daily life indices cache);
#endif
