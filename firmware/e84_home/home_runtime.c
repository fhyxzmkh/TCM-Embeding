#include <board.h>
#include <rtdevice.h>
#include <drivers/i2c-bit-ops.h>
#include <rtthread.h>
#include <rthw.h>
#include <stdlib.h>
#include <cy_rtc.h>
#include "home_data.h"

static home_environment_t latest;
static bool sensor_ok;

/* The driver demo uses P9.3/P9.2 for AHT20. LVGL's i2c1 is wired to touch. */
#define HOME_AHT20_SCL GET_PIN(9, 3)
#define HOME_AHT20_SDA GET_PIN(9, 2)

static struct rt_i2c_bus_device sensor_bus;

static void sensor_set_sda(void *context, rt_int32_t state)
{
    (void)context;
    rt_pin_write(HOME_AHT20_SDA, state);
}

static void sensor_set_scl(void *context, rt_int32_t state)
{
    (void)context;
    rt_pin_write(HOME_AHT20_SCL, state);
}

static rt_int32_t sensor_get_sda(void *context)
{
    (void)context;
    return rt_pin_read(HOME_AHT20_SDA);
}

static rt_int32_t sensor_get_scl(void *context)
{
    (void)context;
    return rt_pin_read(HOME_AHT20_SCL);
}

static const struct rt_i2c_bit_ops sensor_bus_ops = {
    .set_sda = sensor_set_sda, .set_scl = sensor_set_scl,
    .get_sda = sensor_get_sda, .get_scl = sensor_get_scl,
    .udelay = rt_hw_us_delay, .delay_us = 5, .timeout = 100,
};

bool home_environment_get(home_environment_t *value)
{
    rt_base_t irq = rt_hw_interrupt_disable();
    *value = latest;
    const bool valid = sensor_ok;
    rt_hw_interrupt_enable(irq);
    return valid;
}

bool home_clock_get(home_date_t *d)
{
    __attribute__((aligned(32))) cy_stc_rtc_config_t rtc = {0};
    Cy_RTC_GetDateAndTime(&rtc);
    *d = (home_date_t){rtc.year + 2000U, rtc.month, rtc.date, rtc.hour, rtc.min, rtc.sec};
    return home_date_valid(*d);
}

int home_clock_set(home_date_t d)
{
    if (!home_date_valid(d)) return -RT_EINVAL;
    __attribute__((aligned(32))) const cy_stc_rtc_config_t rtc = {
        .sec = d.second, .min = d.minute, .hour = d.hour, .amPm = CY_RTC_AM,
        .hrFormat = CY_RTC_24_HOURS, .dayOfWeek = home_weekday(d) + 1,
        .date = d.day, .month = d.month, .year = d.year - 2000,
    };
    const cy_en_rtc_status_t status = Cy_RTC_SetDateAndTime(&rtc);
    rt_kprintf("TCM_HOME_RTC_SET status=%u\n", (unsigned)status);
    return status == CY_RTC_SUCCESS ? RT_EOK : -RT_ERROR;
}

static const char *measure(struct rt_i2c_bus_device *bus, home_environment_t *sample)
{
    uint8_t status, bytes[7];
    const uint8_t status_cmd[] = {0x71}, init[] = {0xbe, 0x08, 0x00}, measure_cmd[] = {0xac, 0x33, 0x00};
    if (rt_i2c_master_send(bus, 0x38, 0, status_cmd, sizeof(status_cmd)) != sizeof(status_cmd) ||
        rt_i2c_master_recv(bus, 0x38, 0, &status, 1) != 1) return "status";
    if (status & 0x80) return "busy";
    if (!(status & 0x08)) {
        if (rt_i2c_master_send(bus, 0x38, 0, init, sizeof(init)) != sizeof(init)) return "calibration";
        rt_thread_mdelay(12);
    }
    if (rt_i2c_master_send(bus, 0x38, 0, measure_cmd, sizeof(measure_cmd)) != sizeof(measure_cmd)) return "trigger";
    rt_thread_mdelay(85);
    for (unsigned retry = 0; retry < 4; ++retry) {
        if (rt_i2c_master_recv(bus, 0x38, 0, bytes, sizeof(bytes)) != sizeof(bytes)) return "read";
        if (!(bytes[0] & 0x80)) return home_decode_aht20(bytes, sample) ? RT_NULL : "invalid_frame";
        rt_thread_mdelay(10);
    }
    return "busy";
}

static void sensor_thread(void *parameter)
{
    (void)parameter;
    rt_thread_mdelay(100);
    bool first = true, previous = false;
    while (1) {
        home_environment_t sample = {0};
        const char *error = measure(&sensor_bus, &sample);
        const bool ok = error == RT_NULL;
        rt_base_t irq = rt_hw_interrupt_disable();
        latest = sample;
        sensor_ok = ok;
        rt_hw_interrupt_enable(irq);
        if (first || ok != previous) {
            rt_kprintf("TCM_HOME_AHT20 ok=%u temp10=%d rh10=%u state=%s\n", (unsigned)ok,
                       sample.temperature_tenths, sample.humidity_tenths, ok ? "ready" : error);
            first = false;
            previous = ok;
        }
        rt_thread_mdelay(5000);
    }
}

int home_services_start(void)
{
    rt_pin_mode(HOME_AHT20_SCL, PIN_MODE_OUTPUT_OD);
    rt_pin_mode(HOME_AHT20_SDA, PIN_MODE_OUTPUT_OD);
    rt_pin_write(HOME_AHT20_SCL, PIN_HIGH);
    rt_pin_write(HOME_AHT20_SDA, PIN_HIGH);
    sensor_bus.priv = (void *)&sensor_bus_ops;
    int result = rt_i2c_bit_add_bus(&sensor_bus, "i2c_env");
    if (result != RT_EOK) return result;
    rt_thread_t thread = rt_thread_create("homeenv", sensor_thread, RT_NULL, 2048, 23, 10);
    return thread ? rt_thread_startup(thread) : -RT_ERROR;
}

static int home_set_time(int argc, char **argv)
{
    if (argc != 7) {
        rt_kprintf("Usage: home_set_time YYYY MM DD HH MM SS (UTC+08)\n");
        return -RT_ERROR;
    }
    unsigned v[6];
    for (unsigned i = 0; i < 6; ++i) {
        char *end;
        long n = strtol(argv[i + 1], &end, 10);
        if (end == argv[i + 1] || *end || n < 0 || n > 2099) return -RT_ERROR;
        v[i] = (unsigned)n;
    }
    return home_clock_set((home_date_t){v[0], v[1], v[2], v[3], v[4], v[5]});
}
MSH_CMD_EXPORT(home_set_time, set the home RTC in UTC+08);

static int home_status(void)
{
    home_environment_t sample;
    home_date_t date;
    bool valid = home_clock_get(&date), on_term = false;
    const int term = home_solar_term(date, &on_term);
    const bool env = home_environment_get(&sample);
    rt_kprintf("TCM_HOME_STATUS clock=%u %04u-%02u-%02u %02u:%02u:%02u term=%d term_today=%u env=%u temp10=%d rh10=%u\n",
               (unsigned)valid, date.year, date.month, date.day, date.hour, date.minute, date.second,
               term, (unsigned)on_term, (unsigned)env, sample.temperature_tenths, sample.humidity_tenths);
    return 0;
}
MSH_CMD_EXPORT(home_status, read home RTC and AHT20 status);
