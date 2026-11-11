#include <rtthread.h>
#include <finsh.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "mbedtls/ctr_drbg.h"
#include "mbedtls/net.h"
#include "mbedtls/platform.h"
#include "mbedtls/ssl.h"
#include "weather_ca.h"

static unsigned char entropy[48];
static bool entropy_ready;
static time_t clock_epoch;
static rt_tick_t clock_tick;

static int parse_hex(char digit)
{
    if ('0' <= digit && digit <= '9') return digit - '0';
    if ('a' <= digit && digit <= 'f') return digit - 'a' + 10;
    return -1;
}

static time_t tls_time(time_t *value)
{
    time_t now = clock_epoch + (rt_tick_get() - clock_tick) / RT_TICK_PER_SECOND;
    if (value) *value = now;
    return now;
}

static int weather_seed(int argc, char **argv)
{
    if (argc != 3 || strlen(argv[2]) != sizeof(entropy) * 2) return -1;
    entropy_ready = false;
    char *end;
    unsigned long epoch = strtoul(argv[1], &end, 10);
    if (*end || epoch < 1735689600UL || epoch >= 2145916800UL) return -1;
    for (size_t i = 0; i < sizeof(entropy); ++i) {
        int high = parse_hex(argv[2][2 * i]);
        int low = parse_hex(argv[2][2 * i + 1]);
        if (high < 0 || low < 0) return -1;
        entropy[i] = (unsigned char)(high * 16 + low);
    }
    clock_epoch = (time_t)epoch;
    clock_tick = rt_tick_get();
    mbedtls_platform_set_time(tls_time);
    entropy_ready = true;
    rt_kprintf("WEATHER_SEED_READY\n");
    return 0;
}
MSH_CMD_EXPORT(weather_seed, inject validation clock and operating system random seed);

static int take_entropy(void *ignored, unsigned char *output, size_t count)
{
    (void)ignored;
    if (!entropy_ready || count != sizeof(entropy)) return MBEDTLS_ERR_CTR_DRBG_ENTROPY_SOURCE_FAILED;
    memcpy(output, entropy, count);
    memset(entropy, 0, sizeof(entropy));
    entropy_ready = false;
    return 0;
}

static int stream_response(mbedtls_net_context *socket, mbedtls_ssl_context *tls, const char *request)
{
    size_t remaining = strlen(request);
    int result;
    while (remaining) {
        result = tls ? mbedtls_ssl_write(tls, (const unsigned char *)request, remaining) :
                       mbedtls_net_send(socket, (const unsigned char *)request, remaining);
        if (result <= 0) return result ? result : -1;
        request += result;
        remaining -= (size_t)result;
    }
    rt_kprintf("WEATHER_WIRE_BEGIN\n");
    unsigned total = 0;
    unsigned char bytes[96];
    const char digits[] = "0123456789abcdef";
    do {
        result = tls ? mbedtls_ssl_read(tls, bytes, sizeof(bytes)) :
                       mbedtls_net_recv_timeout(socket, bytes, sizeof(bytes), 7000);
        if (result > 0) {
            total += (unsigned)result;
            if (total > 16384) return -1;
            char hex[sizeof(bytes) * 2 + 1];
            for (int i = 0; i < result; ++i) {
                hex[2 * i] = digits[bytes[i] >> 4];
                hex[2 * i + 1] = digits[bytes[i] & 15];
            }
            hex[2 * result] = 0;
            rt_kprintf("W:%s\n", hex);
        }
    } while (result > 0);
    if (result != 0 && result != MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY) return result;
    rt_kprintf("WEATHER_WIRE_END bytes=%u\n", total);
    return total ? 0 : -1;
}

static int weather_wire(int argc, char **argv)
{
    const bool location = argc == 2 && strcmp(argv[1], "ip") == 0;
    if (!location && (argc != 3 || !entropy_ready)) return -1;
    const char *host = location ? "ip-api.com" : WEATHER_HOST;
    const char *path = location ? "/json/?fields=status,countryCode,regionName,city,lat,lon,proxy&lang=zh-CN" : argv[1];
    if (!location && strncmp(path, "/weather/v1/", 12) != 0) return -1;
    char request[448];
    int length;
    if (location)
        length = rt_snprintf(request, sizeof(request), "GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n", path, host);
    else
        length = rt_snprintf(request, sizeof(request),
            "GET %s HTTP/1.0\r\nHost: %s\r\nX-QW-Api-Key: %s\r\nAccept-Encoding: gzip\r\nConnection: close\r\n\r\n",
            path, host, argv[2]);
    if (length < 1 || (size_t)length >= sizeof(request)) return -1;

    mbedtls_net_context socket;
    mbedtls_net_init(&socket);
    int result = mbedtls_net_connect(&socket, host, location ? "80" : "443", MBEDTLS_NET_PROTO_TCP);
    if (result != 0 || location) {
        if (result == 0) result = stream_response(&socket, NULL, request);
        mbedtls_net_free(&socket);
        if (result != 0) rt_kprintf("WEATHER_WIRE_ERROR status=%d\n", result);
        return result;
    }

    mbedtls_ctr_drbg_context random;
    mbedtls_x509_crt ca;
    mbedtls_ssl_config config;
    mbedtls_ssl_context tls;
    mbedtls_ctr_drbg_init(&random);
    mbedtls_x509_crt_init(&ca);
    mbedtls_ssl_config_init(&config);
    mbedtls_ssl_init(&tls);
    mbedtls_ctr_drbg_set_entropy_len(&random, sizeof(entropy));
    result = mbedtls_ctr_drbg_seed(&random, take_entropy, NULL, NULL, 0);
    if (result != 0) goto done;
    result = mbedtls_x509_crt_parse(&ca, weather_ca, sizeof(weather_ca));
    if (result != 0) goto done;
    result = mbedtls_ssl_config_defaults(&config, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM,
                                        MBEDTLS_SSL_PRESET_DEFAULT);
    if (result != 0) goto done;
    mbedtls_ssl_conf_authmode(&config, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_min_version(&config, MBEDTLS_SSL_MAJOR_VERSION_3, MBEDTLS_SSL_MINOR_VERSION_3);
    mbedtls_ssl_conf_ca_chain(&config, &ca, NULL);
    mbedtls_ssl_conf_rng(&config, mbedtls_ctr_drbg_random, &random);
    mbedtls_ssl_conf_read_timeout(&config, 8000);
    result = mbedtls_ssl_setup(&tls, &config);
    if (result != 0) goto done;
    result = mbedtls_ssl_set_hostname(&tls, host);
    if (result != 0) goto done;
    mbedtls_ssl_set_bio(&tls, &socket, mbedtls_net_send, mbedtls_net_recv, mbedtls_net_recv_timeout);
    result = mbedtls_ssl_handshake(&tls);
    if (result != 0) goto done;
    rt_kprintf("WEATHER_TLS_OK version=%s\n", mbedtls_ssl_get_version(&tls));
    result = stream_response(&socket, &tls, request);
done:
    if (result != 0)
        rt_kprintf("WEATHER_WIRE_ERROR status=%d verify=%u\n", result, (unsigned)mbedtls_ssl_get_verify_result(&tls));
    mbedtls_net_free(&socket);
    mbedtls_ssl_free(&tls);
    mbedtls_ssl_config_free(&config);
    mbedtls_x509_crt_free(&ca);
    mbedtls_ctr_drbg_free(&random);
    return result;
}
MSH_CMD_EXPORT(weather_wire, retrieve the IP or weather response);
