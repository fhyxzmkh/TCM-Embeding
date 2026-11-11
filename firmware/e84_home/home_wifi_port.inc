/* Included in the staged WHD station driver to reuse its interface and events. */
#include "whd_wlioctl.h"
#include "home_weather.h"

static struct rt_semaphore scan_done;
static whd_scan_result_t beacon, selected_ap;
static whd_scan_status_t scan_status;
static char scan_ssid[33];
static bool scan_initialized, scan_pending;

static void home_beacon(whd_scan_result_t **result, void *context, whd_scan_status_t status)
{
    (void)context;
    if (status == WHD_SCAN_COMPLETED_SUCCESSFULLY || status == WHD_SCAN_ABORTED) {
        scan_status = status;
        rt_sem_release(&scan_done);
        return;
    }
    if (!result || !*result) return;
    whd_scan_result_t *entry = *result;
    size_t length = rt_strlen(scan_ssid);
    if (entry->band == WHD_802_11_BAND_2_4GHZ && entry->channel >= 1 && entry->channel <= 13 &&
        entry->SSID.length == length && rt_memcmp(entry->SSID.value, scan_ssid, length) == 0 &&
        (!selected_ap.channel || entry->signal_strength > selected_ap.signal_strength)) {
        selected_ap = *entry;
    }
    rt_memset(entry, 0, sizeof(*entry));
}

static rt_err_t home_wifi_join_limited(struct rt_wlan_device *wlan, struct rt_sta_info *sta)
{
    whd_interface_t iface = wifi_sta.whd_itf;
    if (wlan != wifi_sta.wlan || selected_ap.band != WHD_802_11_BAND_2_4GHZ ||
        selected_ap.channel < 1 || selected_ap.channel > 13 || selected_ap.channel != sta->channel ||
        selected_ap.SSID.length != sta->ssid.len ||
        rt_memcmp(selected_ap.SSID.value, sta->ssid.val, sta->ssid.len) != 0 ||
        rt_memcmp(selected_ap.BSSID.octet, sta->bssid, sizeof(sta->bssid)) != 0) return -RT_EINVAL;

    uint32_t band = 0, power = 0;
    const uint32_t limit_qdbm = 20;
    whd_result_t status = whd_wifi_set_ioctl_value(iface, WLC_SET_BAND, WLC_BAND_2G);
    if (status == WHD_SUCCESS) status = whd_wifi_get_ioctl_value(iface, WLC_GET_BAND, &band);
    if (status != WHD_SUCCESS || band != WLC_BAND_2G) return -RT_ERROR;
    status = whd_wifi_set_iovar_value(iface, IOVAR_STR_QTXPOWER, limit_qdbm);
    if (status == WHD_SUCCESS) status = whd_wifi_get_iovar_value(iface, IOVAR_STR_QTXPOWER, &power);
    if (status == WHD_SUCCESS && (power & ~WL_TXPWR_OVERRIDE) > limit_qdbm) {
        status = whd_wifi_set_iovar_value(iface, IOVAR_STR_QTXPOWER, limit_qdbm | WL_TXPWR_OVERRIDE);
        if (status == WHD_SUCCESS) status = whd_wifi_get_iovar_value(iface, IOVAR_STR_QTXPOWER, &power);
    }
    if (status != WHD_SUCCESS || (power & ~WL_TXPWR_OVERRIDE) > limit_qdbm) return -RT_ERROR;
    status = whd_wifi_set_iovar_value(iface, IOVAR_STR_ROAM_OFF, 1);
    if (status != WHD_SUCCESS) return -RT_ERROR;

    status = whd_wifi_join_specific(iface, &selected_ap, (const uint8_t *)sta->key.val, sta->key.len);
    if (status != WHD_SUCCESS) {
        rt_wlan_dev_indicate_event_handle(wlan, RT_WLAN_DEV_EVT_CONNECT_FAIL, RT_NULL);
        return -RT_ERROR;
    }
    uint32_t actual = 0;
    if (whd_wifi_get_channel(iface, &actual) != WHD_SUCCESS || actual != selected_ap.channel) {
        whd_wifi_leave(iface);
        return -RT_ERROR;
    }
    return RT_EOK;
}

bool home_wifi_ready(void)
{
    return rt_wlan_is_connected() && rt_wlan_is_ready();
}

int home_wifi_connect(const char *ssid, const char *password)
{
    rt_tick_t startup_deadline = rt_tick_get() + rt_tick_from_millisecond(6000);
    while (rt_wlan_get_mode(RT_WLAN_DEVICE_STA_NAME) != RT_WLAN_STATION) {
        if ((rt_int32_t)(startup_deadline - rt_tick_get()) <= 0) return -RT_ETIMEOUT;
        rt_thread_mdelay(100);
    }
    if (rt_wlan_is_connected() && rt_wlan_disconnect() != RT_EOK) return -RT_ERROR;
    if (!scan_initialized) {
        if (rt_sem_init(&scan_done, "homeap", 0, RT_IPC_FLAG_FIFO) != RT_EOK) return -RT_ERROR;
        scan_initialized = true;
    }
    if (scan_pending && rt_sem_trytake(&scan_done) != RT_EOK) return -RT_EBUSY;
    scan_pending = false;
    rt_strncpy(scan_ssid, ssid, sizeof(scan_ssid) - 1);
    rt_memset(&selected_ap, 0, sizeof(selected_ap));
    rt_memset(&beacon, 0, sizeof(beacon));
    static const uint16_t channels[] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 0};
    static const whd_scan_extended_params_t params = {
        .number_of_probes_per_channel = 0,
        .scan_active_dwell_time_per_channel_ms = -1,
        .scan_passive_dwell_time_per_channel_ms = 300,
        .scan_home_channel_dwell_time_between_channels_ms = -1,
    };
    whd_result_t status = whd_wifi_scan(wifi_sta.whd_itf, WHD_SCAN_TYPE_PASSIVE,
        WHD_BSS_TYPE_INFRASTRUCTURE, RT_NULL, RT_NULL, channels, &params, home_beacon, &beacon, RT_NULL);
    if (status != WHD_SUCCESS) return -RT_ERROR;
    if (rt_sem_take(&scan_done, rt_tick_from_millisecond(12000)) != RT_EOK) {
        scan_pending = true;
        whd_wifi_stop_scan(wifi_sta.whd_itf);
        return -RT_ETIMEOUT;
    }
    if (scan_status != WHD_SCAN_COMPLETED_SUCCESSFULLY || !selected_ap.channel) return -RT_ERROR;

    struct rt_wlan_info info;
    INVALID_INFO(&info);
    info.ssid.len = selected_ap.SSID.length;
    rt_memcpy(info.ssid.val, selected_ap.SSID.value, info.ssid.len);
    rt_memcpy(info.bssid, selected_ap.BSSID.octet, sizeof(info.bssid));
    info.channel = selected_ap.channel;
    info.band = RT_802_11_BAND_2_4GHZ;
    info.security = (rt_wlan_security_t)selected_ap.security;
    rt_err_t error = rt_wlan_connect_adv(&info, password);
    if (error != RT_EOK) return error;
    rt_tick_t deadline = rt_tick_get() + rt_tick_from_millisecond(25000);
    while (!home_wifi_ready()) {
        if ((rt_int32_t)(deadline - rt_tick_get()) <= 0) return -RT_ETIMEOUT;
        rt_thread_mdelay(250);
    }
    rt_kprintf("TCM_WEATHER_NETWORK_READY channel=%u\n", info.channel);
    return 0;
}
