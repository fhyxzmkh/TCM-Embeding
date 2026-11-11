#include <board.h>
#include <rtthread.h>
#include <rtdevice.h>
#include <rthw.h>
#include "home_data.h"
#ifdef TCM_HOME_WEATHER
#include "home_weather.h"
#endif

extern int lvgl_thread_init(void);

int main(void)
{
    rt_kprintf("TCM_HOME_BOOT\n");
#if defined(BSP_LVGL_ENABLE_CPU_CACHE) && defined(RT_USING_CACHE)
    if (!rt_hw_cpu_icache_status()) rt_hw_cpu_icache_enable();
    if (!rt_hw_cpu_dcache_status()) rt_hw_cpu_dcache_enable();
#endif
    if (home_services_start() != RT_EOK) rt_kprintf("TCM_HOME_SENSOR_START_FAILED\n");
#ifdef TCM_HOME_WEATHER
    home_weather_start();
#endif
    rt_thread_mdelay(1500);
    if (lvgl_thread_init() != 0) {
        rt_kprintf("TCM_HOME_GUI_START_FAILED\n");
        return -1;
    }
    rt_thread_mdelay(300);
    rt_pin_mode(GET_PIN(15, 7), PIN_MODE_OUTPUT);
    rt_pin_mode(GET_PIN(20, 6), PIN_MODE_OUTPUT);
    rt_pin_write(GET_PIN(15, 7), PIN_HIGH);
    rt_pin_write(GET_PIN(20, 6), PIN_HIGH);
    return 0;
}
