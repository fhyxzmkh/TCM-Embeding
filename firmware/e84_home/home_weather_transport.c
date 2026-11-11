#include <rtconfig.h>
#ifdef TCM_HOME_WEATHER
#include <board.h>
#include <cy_crypto_core_hw.h>
#include <cy_crypto_core_trng.h>
#include <lvgl.h>
#include <lodepng.h>
#include <cJSON.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/net.h>
#include <mbedtls/platform.h>
#include <mbedtls/ssl.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netdb.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#include "home_weather.h"
#include "weather_root.h"
#include "home_city_aliases.h"

#define RESPONSE_CAPACITY 12288
#define JSON_CAPACITY 32768

static time_t weather_time(time_t *output)
{
    home_date_t date;
    if (!home_clock_get(&date)) return (time_t)-1;
    static const unsigned days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    unsigned elapsed = date.day - 1;
    for (unsigned y = 2000; y < date.year; ++y) elapsed += 365 + (y % 4 == 0);
    for (unsigned m = 1; m < date.month; ++m) elapsed += days[m - 1] + (m == 2 && date.year % 4 == 0);
    int64_t seconds = 946684800LL + elapsed * 86400LL + date.hour * 3600 +
                      date.minute * 60 + date.second - 8 * 3600;
    time_t result = (time_t)seconds;
    if ((int64_t)result != seconds) return (time_t)-1;
    if (output) *output = result;
    return result;
}

static int hardware_entropy(void *context, unsigned char *output, size_t count)
{
    (void)context;
    static bool enabled;
    if (!enabled) {
        if (Cy_Crypto_Core_Enable(CRYPTO) != CY_CRYPTO_SUCCESS) return -1;
        enabled = true;
    }
    while (count) {
        uint32_t random;
        if (Cy_Crypto_Core_Trng_Ext(CRYPTO, 32, &random) != CY_CRYPTO_SUCCESS) return -1;
        size_t size = count < sizeof(random) ? count : sizeof(random);
        memcpy(output, &random, size);
        output += size;
        count -= size;
    }
    return 0;
}

static bool query_time(const char *host, home_date_t *date)
{
    enum { NTP_SIZE = 48, ORIGIN = 24, TRANSMIT = 40 };
    unsigned char request[NTP_SIZE] = {0x23}; /* NTPv4 client */
    if (hardware_entropy(NULL, request + TRANSMIT, 8) != 0) return false;

    struct addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_DGRAM};
    struct addrinfo *address = NULL;
    if (getaddrinfo(host, "123", &hints, &address) != 0) return false;
    int fd = socket(address->ai_family, address->ai_socktype, address->ai_protocol);
    bool sent = fd >= 0 && connect(fd, address->ai_addr, address->ai_addrlen) == 0 &&
                send(fd, request, sizeof(request), 0) == (int)sizeof(request);
    freeaddrinfo(address);
    unsigned char reply[NTP_SIZE];
    int received = -1;
    if (sent) {
        fd_set readable;
        FD_ZERO(&readable);
        FD_SET(fd, &readable);
        struct timeval timeout = {.tv_sec = 3};
        if (select(fd + 1, &readable, NULL, NULL, &timeout) == 1)
            received = recv(fd, reply, sizeof(reply), 0);
    }
    if (fd >= 0) close(fd);
    if (received != NTP_SIZE || (reply[0] & 7) != 4 || (reply[0] >> 6) == 3 ||
        ((reply[0] >> 3) & 7) < 3 || ((reply[0] >> 3) & 7) > 4 ||
        reply[1] == 0 || reply[1] > 15 || memcmp(reply + ORIGIN, request + TRANSMIT, 8) != 0)
        return false;

    uint32_t seconds = (uint32_t)reply[TRANSMIT] << 24 | (uint32_t)reply[TRANSMIT + 1] << 16 |
                       (uint32_t)reply[TRANSMIT + 2] << 8 | reply[TRANSMIT + 3];
    if (!seconds && memcmp(reply + TRANSMIT + 4, "\0\0\0\0", 4) == 0) return false;
    /* Unsigned subtraction unfolds the NTP rollover into Unix seconds through 2106. */
    uint64_t local_seconds = (uint32_t)(seconds - 2208988800U) + 28800ULL;
    time_t local = (time_t)local_seconds;
    struct tm parts;
    if ((uint64_t)local != local_seconds || !gmtime_r(&local, &parts)) return false;
    *date = (home_date_t){parts.tm_year + 1900, parts.tm_mon + 1, parts.tm_mday,
                          parts.tm_hour, parts.tm_min, parts.tm_sec};
    return home_date_valid(*date);
}

bool home_network_time(home_date_t *date)
{
    static const char *const servers[] = {"ntp.aliyun.com", "ntp.tencent.com"};
    for (unsigned i = 0; i < sizeof(servers) / sizeof(servers[0]); ++i) {
        if (query_time(servers[i], date)) {
            rt_kprintf("TCM_HOME_NTP_SYNC server=%s\n", servers[i]);
            return true;
        }
    }
    rt_kprintf("TCM_HOME_NTP_RETRY\n");
    return false;
}

static const char *find_header(const char *headers, const char *name)
{
    size_t n = strlen(name);
    const char *line = headers;
    while ((line = strstr(line, "\r\n"))) {
        line += 2;
        if (strncasecmp(line, name, n) != 0 || line[n] != ':') continue;
        line += n + 1;
        while (*line == ' ' || *line == '\t') ++line;
        return line;
    }
    return NULL;
}

static bool header_value(const char *headers, const char *name, const char *value)
{
    const char *line = find_header(headers, name);
    size_t length = strlen(value);
    return line && strncasecmp(line, value, length) == 0 && (line[length] == '\r' || line[length] == 0);
}

static size_t unchunk(unsigned char *body, size_t length)
{
    unsigned char *reader = body, *writer = body, *end = body + length;
    while (reader < end) {
        char *after;
        unsigned long chunk = strtoul((char *)reader, &after, 16);
        if ((unsigned char *)after == reader || end - (unsigned char *)after < 2 ||
            after[0] != '\r' || after[1] != '\n') return 0;
        reader = (unsigned char *)after + 2;
        if (chunk == 0) return (size_t)(writer - body);
        if ((size_t)(end - reader) < 2 || chunk > (size_t)(end - reader) - 2 ||
            reader[chunk] != '\r' || reader[chunk + 1] != '\n') return 0;
        memmove(writer, reader, chunk);
        writer += chunk;
        reader += chunk + 2;
    }
    return 0;
}

static uint32_t load_le32(const unsigned char *bytes)
{
    return (uint32_t)bytes[0] | (uint32_t)bytes[1] << 8 |
           (uint32_t)bytes[2] << 16 | (uint32_t)bytes[3] << 24;
}

static uint32_t gzip_crc(const unsigned char *bytes, size_t size)
{
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < size; ++i) {
        crc ^= bytes[i];
        for (unsigned b = 0; b < 8; ++b) crc = (crc >> 1) ^ (crc & 1 ? 0xedb88320U : 0);
    }
    return ~crc;
}

static cJSON *parse_gzip(const unsigned char *body, size_t size)
{
    if (size < 18 || body[0] != 0x1f || body[1] != 0x8b || body[2] != 8 || (body[3] & 0xe0))
        return NULL;
    const unsigned char *start = body + 10, *trailer = body + size - 8;
    unsigned flags = body[3];
    if (flags & 4) {
        if (trailer - start < 2) return NULL;
        size_t extra = start[0] + 256U * start[1];
        start += 2;
        if (extra > (size_t)(trailer - start)) return NULL;
        start += extra;
    }
    for (unsigned flag = 8; flag <= 16; flag <<= 1) {
        if (flags & flag) {
            start = memchr(start, 0, (size_t)(trailer - start));
            if (!start) return NULL;
            ++start;
        }
    }
    if (flags & 2) {
        if (trailer - start < 2) return NULL;
        start += 2;
    }
    size_t expected = load_le32(trailer + 4);
    if (start >= trailer || !expected || expected > JSON_CAPACITY) return NULL;
    LodePNGDecompressSettings settings;
    lodepng_decompress_settings_init(&settings);
    settings.max_output_size = expected;
    unsigned char *output = NULL;
    size_t actual = 0;
    unsigned error = lodepng_inflate(&output, &actual, start, (size_t)(trailer - start), &settings);
    cJSON *json = NULL;
    if (!error && actual == expected && gzip_crc(output, actual) == load_le32(trailer))
        json = cJSON_ParseWithLength((const char *)output, actual);
    lv_free(output);
    return json;
}

static cJSON *parse_response(unsigned char *response, size_t length, unsigned *cooldown)
{
    char *headers = (char *)response;
    char *boundary = strstr(headers, "\r\n\r\n");
    if (!boundary || strncmp(headers, "HTTP/1.", 7) != 0) return NULL;
    *boundary = 0;
    bool throttled = strncmp(headers + 8, " 429 ", 5) == 0;
    if (cooldown && (throttled || header_value(headers, "X-Rl", "0"))) {
        const char *ttl = find_header(headers, "X-Ttl");
        unsigned long seconds = ttl ? strtoul(ttl, NULL, 10) : 60;
        *cooldown = seconds > 86400 ? 86400 : (unsigned)seconds + 1;
    }
    if (strncmp(headers + 8, " 200 ", 5) != 0) return NULL;
    unsigned char *body = (unsigned char *)boundary + 4;
    size_t size = length - (size_t)(body - response);
    if (header_value(headers, "Transfer-Encoding", "chunked")) {
        size = unchunk(body, size);
    } else {
        const char *content_length = find_header(headers, "Content-Length");
        if (content_length && strtoul(content_length, NULL, 10) != size) return NULL;
    }
    if (!size) return NULL;
    if (header_value(headers, "Content-Encoding", "gzip")) return parse_gzip(body, size);
    return cJSON_ParseWithLength((const char *)body, size);
}

static int connect_bounded(mbedtls_net_context *net, const char *host, const char *port)
{
    struct addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_STREAM};
    struct addrinfo *addresses = NULL;
    if (getaddrinfo(host, port, &hints, &addresses) != 0) return MBEDTLS_ERR_NET_UNKNOWN_HOST;
    int result = MBEDTLS_ERR_NET_CONNECT_FAILED;
    for (struct addrinfo *entry = addresses; entry; entry = entry->ai_next) {
        net->fd = socket(entry->ai_family, entry->ai_socktype, entry->ai_protocol);
        if (net->fd < 0) continue;
        if (fcntl(net->fd, F_SETFL, O_NONBLOCK) != 0) {
            mbedtls_net_free(net);
            continue;
        }
        int connected = connect(net->fd, entry->ai_addr, entry->ai_addrlen);
        if (connected != 0 && (errno == EINPROGRESS || errno == EWOULDBLOCK)) {
            fd_set writable;
            FD_ZERO(&writable);
            FD_SET(net->fd, &writable);
            struct timeval timeout = {.tv_sec = 4};
            if (select(net->fd + 1, NULL, &writable, NULL, &timeout) == 1) {
                int error = 0;
                socklen_t size = sizeof(error);
                if (getsockopt(net->fd, SOL_SOCKET, SO_ERROR, &error, &size) == 0 && !error)
                    connected = 0;
            }
        }
        if (connected == 0 && fcntl(net->fd, F_SETFL, 0) == 0) {
            struct timeval timeout = {.tv_sec = 5};
            setsockopt(net->fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
            result = 0;
            break;
        }
        mbedtls_net_free(net);
    }
    freeaddrinfo(addresses);
    return result;
}

static int http_location(cJSON **data)
{
    static const unsigned char request[] =
        "GET /json/?fields=status,countryCode,city,lat,lon,proxy,hosting&lang=zh-CN HTTP/1.0\r\n"
        "Host: ip-api.com\r\nUser-Agent: TCM-EdgiTalk/1.0\r\nConnection: close\r\n\r\n";
    static rt_tick_t next_request;
    static bool limited;
    if (limited && (rt_int32_t)(next_request - rt_tick_get()) > 0) return -110;
    limited = true;
    next_request = rt_tick_get() + rt_tick_from_millisecond(60000);

    mbedtls_net_context net;
    mbedtls_net_init(&net);
    unsigned char response[2049];
    size_t used = 0;
    int status = connect_bounded(&net, "ip-api.com", "80");
    if (status) goto done;
    for (size_t offset = 0; offset < sizeof(request) - 1; offset += (size_t)status) {
        status = mbedtls_net_send(&net, request + offset, sizeof(request) - 1 - offset);
        if (status <= 0) {
            if (!status) status = -111;
            goto done;
        }
    }
    while (used < sizeof(response) - 1) {
        status = mbedtls_net_recv_timeout(&net, response + used, sizeof(response) - 1 - used, 4000);
        if (status <= 0) break;
        used += (size_t)status;
    }
    status = -111;
    if (used && used < sizeof(response) - 1) {
        response[used] = 0;
        unsigned cooldown = 0;
        *data = parse_response(response, used, &cooldown);
        if (cooldown > 60) next_request = rt_tick_get() + rt_tick_from_millisecond((rt_int32_t)cooldown * 1000);
        if (*data) status = 0;
    }
done:
    mbedtls_net_free(&net);
    return status;
}

static int https_json(const char *host, const char *key, const char *path, cJSON **data)
{
    mbedtls_net_context socket;
    mbedtls_ctr_drbg_context random;
    mbedtls_x509_crt ca;
    mbedtls_ssl_config config;
    mbedtls_ssl_context tls;
    mbedtls_net_init(&socket);
    mbedtls_ctr_drbg_init(&random);
    mbedtls_x509_crt_init(&ca);
    mbedtls_ssl_config_init(&config);
    mbedtls_ssl_init(&tls);
    unsigned char *response = rt_malloc(RESPONSE_CAPACITY + 1);
    char request[384];
    int status = -100;
    if (!response) goto done;
    int length = rt_snprintf(request, sizeof(request),
        "GET %s HTTP/1.0\r\nHost: %s\r\n%s%s%sUser-Agent: TCM-EdgiTalk/1.0\r\n"
        "Accept-Encoding: gzip\r\nConnection: close\r\n\r\n",
        path, host, key ? "X-QW-Api-Key: " : "", key ? key : "", key ? "\r\n" : "");
    if (length <= 0 || (size_t)length >= sizeof(request)) goto done;

    mbedtls_platform_set_time(weather_time);
    mbedtls_ctr_drbg_set_entropy_len(&random, 48);
    status = mbedtls_ctr_drbg_seed(&random, hardware_entropy, NULL, NULL, 0);
    if (status) goto done;
    status = mbedtls_x509_crt_parse(&ca, key ? weather_ca : geo_ca, key ? sizeof(weather_ca) : sizeof(geo_ca));
    if (status) goto done;
    status = mbedtls_ssl_config_defaults(&config, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM,
                                        MBEDTLS_SSL_PRESET_DEFAULT);
    if (status) goto done;
    mbedtls_ssl_conf_authmode(&config, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_min_version(&config, MBEDTLS_SSL_MAJOR_VERSION_3, MBEDTLS_SSL_MINOR_VERSION_3);
    mbedtls_ssl_conf_ca_chain(&config, &ca, NULL);
    mbedtls_ssl_conf_rng(&config, mbedtls_ctr_drbg_random, &random);
    mbedtls_ssl_conf_read_timeout(&config, 8000);
    status = mbedtls_ssl_setup(&tls, &config);
    if (status) goto done;
    status = mbedtls_ssl_set_hostname(&tls, host);
    if (status) goto done;
    status = connect_bounded(&socket, host, "443");
    if (status == MBEDTLS_ERR_NET_UNKNOWN_HOST) {
        rt_thread_mdelay(1500);
        status = connect_bounded(&socket, host, "443");
    }
    if (status) goto done;
    mbedtls_ssl_set_bio(&tls, &socket, mbedtls_net_send, mbedtls_net_recv, mbedtls_net_recv_timeout);
    status = mbedtls_ssl_handshake(&tls);
    if (status) goto done;
    for (int offset = 0; offset < length; offset += status) {
        status = mbedtls_ssl_write(&tls, (const unsigned char *)request + offset, (size_t)(length - offset));
        if (status <= 0) {
            if (!status) status = -101;
            goto done;
        }
    }
    size_t used = 0;
    while (used < RESPONSE_CAPACITY) {
        status = mbedtls_ssl_read(&tls, response + used, RESPONSE_CAPACITY - used);
        if (status == 0 || status == MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY) break;
        if (status < 0) goto done;
        used += (size_t)status;
    }
    status = -102;
    if (used < RESPONSE_CAPACITY) {
        response[used] = 0;
        *data = parse_response(response, used, NULL);
        if (*data) status = 0;
    }
done:
    memset(request, 0, sizeof(request));
    rt_free(response);
    mbedtls_net_free(&socket);
    mbedtls_ssl_free(&tls);
    mbedtls_ssl_config_free(&config);
    mbedtls_x509_crt_free(&ca);
    mbedtls_ctr_drbg_free(&random);
    return status;
}

static home_location_state_t parse_location(cJSON *data, bool primary, home_location_t *location)
{
    if (primary) {
        cJSON *status = cJSON_GetObjectItemCaseSensitive(data, "status");
        if (!cJSON_IsString(status) || strcmp(status->valuestring, "success") != 0)
            return HOME_LOCATION_UNAVAILABLE;
        if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(data, "proxy")) ||
            cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(data, "hosting"))) return HOME_LOCATION_PROXY;
    }
    cJSON *country = cJSON_GetObjectItemCaseSensitive(data, primary ? "countryCode" : "country_code");
    if (!cJSON_IsString(country) || strlen(country->valuestring) != 2) return HOME_LOCATION_UNAVAILABLE;
    if (strcmp(country->valuestring, "CN") != 0) return HOME_LOCATION_OUTSIDE;
    cJSON *lat = cJSON_GetObjectItemCaseSensitive(data, primary ? "lat" : "latitude");
    cJSON *lon = cJSON_GetObjectItemCaseSensitive(data, primary ? "lon" : "longitude");
    if (!cJSON_IsNumber(lat) || !isfinite(lat->valuedouble) || lat->valuedouble < 17 || lat->valuedouble > 55 ||
        !cJSON_IsNumber(lon) || !isfinite(lon->valuedouble) || lon->valuedouble < 73 || lon->valuedouble > 136)
        return HOME_LOCATION_UNAVAILABLE;

    unsigned north = (unsigned)(lat->valuedouble * 100 + 0.5);
    unsigned east = (unsigned)(lon->valuedouble * 100 + 0.5);
    rt_snprintf(location->coordinates, sizeof(location->coordinates), "%u.%02u/%u.%02u",
                north / 100, north % 100, east / 100, east % 100);
    cJSON *city = cJSON_GetObjectItemCaseSensitive(data, "city");
    if (cJSON_IsString(city)) {
        const char *name = city->valuestring;
        for (size_t i = 0; i < sizeof(home_city_aliases) / sizeof(home_city_aliases[0]); ++i) {
            if (strcasecmp(name, home_city_aliases[i].en) == 0) {
                name = home_city_aliases[i].zh;
                break;
            }
        }
        if (strlen(name) < sizeof(location->name) && !strpbrk(name, "\r\n\t"))
            strcpy(location->name, name);
    }
    return primary ? HOME_LOCATION_IP_API : HOME_LOCATION_IP_SB;
}

void home_location_request(home_location_t *location)
{
    *location = (home_location_t){.state = HOME_LOCATION_UNAVAILABLE};
    cJSON *data = NULL;
    int result = http_location(&data);
    if (result == 0) location->state = parse_location(data, true, location);
    cJSON_Delete(data);
    rt_kprintf("TCM_GEO_IP_API status=%d outcome=%u\n", result, (unsigned)location->state);
    if (location->state != HOME_LOCATION_UNAVAILABLE) return;

    data = NULL;
    result = https_json("api.ip.sb", NULL, "/geoip", &data);
    if (result == 0) location->state = parse_location(data, false, location);
    cJSON_Delete(data);
    rt_kprintf("TCM_GEO_IP_SB status=%d outcome=%u\n", result, (unsigned)location->state);
}

static bool read_temperature(cJSON *object, const char *name, int *result)
{
    cJSON *value = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(object, name), "value");
    if (!cJSON_IsNumber(value) || value->valuedouble < -100 || value->valuedouble > 100) return false;
    double raw = value->valuedouble;
    *result = (int)(raw >= 0 ? raw + 0.5 : raw - 0.5);
    return true;
}

static unsigned read_condition(cJSON *object)
{
    cJSON *code = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(object, "condition"), "code");
    if (cJSON_IsString(code)) return (unsigned)strtoul(code->valuestring, NULL, 10);
    return 0;
}

int home_weather_request(const char *host, const char *key, const char *coords, home_weather_t *value)
{
    char path[112];
    cJSON *current = NULL, *forecast = NULL;
    rt_snprintf(path, sizeof(path), "/weather/v1/current/%s?lang=zh&localTime=true", coords);
    int error = https_json(host, key, path, &current);
    if (error) goto done;
    value->condition = read_condition(current);
    error = -103;
    if (!value->condition || !read_temperature(current, "temperature", &value->temperature)) goto done;

    rt_snprintf(path, sizeof(path), "/weather/v1/daily/%s?days=3&lang=zh&localTime=true", coords);
    error = https_json(host, key, path, &forecast);
    if (error) goto done;
    error = -104;
    cJSON *days = cJSON_GetObjectItemCaseSensitive(forecast, "days");
    if (!cJSON_IsArray(days) || cJSON_GetArraySize(days) != 3) goto done;
    for (unsigned i = 0; i < 3; ++i) {
        cJSON *day = cJSON_GetArrayItem(days, i);
        home_forecast_t *out = &value->days[i];
        cJSON *date = cJSON_GetObjectItemCaseSensitive(day, "forecastStartTime");
        unsigned year;
        if (!cJSON_IsString(date) || sscanf(date->valuestring, "%u-%u-%u", &year, &out->month, &out->day) != 3 ||
            !home_date_valid((home_date_t){year, out->month, out->day, 0, 0, 0}) ||
            !read_temperature(day, "temperatureMin", &out->low) ||
            !read_temperature(day, "temperatureMax", &out->high)) goto done;
        out->condition = read_condition(cJSON_GetObjectItemCaseSensitive(day, "daytime"));
    }
    error = 0;
done:
    cJSON_Delete(current);
    cJSON_Delete(forecast);
    return error;
}

int home_indices_request(const char *host, const char *key, const char *coordinates,
                         home_date_t date, home_indices_t *value)
{
    const char *slash = strchr(coordinates, '/');
    if (!slash || strlen(coordinates) >= sizeof(value->coordinates) || slash == coordinates ||
        !slash[1] || strchr(slash + 1, '/') || strspn(coordinates, "0123456789.-/") != strlen(coordinates)) return -105;
    char path[128];
    rt_snprintf(path, sizeof(path), "/v7/indices/1d?type=1,3,8&location=%s,%.*s&lang=zh",
                slash + 1, (int)(slash - coordinates), coordinates);
    cJSON *data = NULL;
    int error = https_json(host, key, path, &data);
    if (!error) error = home_indices_parse(data, date, value);
    cJSON_Delete(data);
    if (!error) strcpy(value->coordinates, coordinates);
    return error;
}
#endif
