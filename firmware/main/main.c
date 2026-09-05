/*
 * AVC ESP32-S3 firmware — sensor acquisition + UDP transport (skeleton).
 *
 * Architecture (TRD section 4):
 *   - One acquisition task paces everything off the 16 kHz mic clock
 *     (16 ms I2S chunks). Piezo (1 kHz) fires every 16th mic sample,
 *     pressure/airflow (100 Hz) every 160th.
 *   - Every 8000 mic samples (= 500 ms window) the task assembles an
 *     avc_sensor_block_t per sensor, serializes via avc_packet_build()
 *     (firmware/main/packet.c — lockstep with services/ingest.py), and
 *     sends one UDP datagram to the gateway host.
 *
 * Sensor drivers: until real pinouts are locked (see README), all four
 * sensors feed from a synthetic generator so the full packet → WiFi →
 * UDP → Python parse path can be validated with only the board present.
 * Replace the AVC_SENSOR_REAL_* hooks in sensors.c-equivalent sections
 * below as hardware lands.
 *
 * Requires ESP-IDF v5.x (esp_wifi, lwip sockets, esp_timer).
 */
#include <errno.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"

#include "packet.h"

/* ---- configuration (firmware/main/Kconfig.projbuild; fallbacks shown) */

#ifdef CONFIG_AVC_WIFI_SSID
#define AVC_WIFI_SSID CONFIG_AVC_WIFI_SSID
#else
#define AVC_WIFI_SSID "CHANGE_ME"
#endif
#ifdef CONFIG_AVC_WIFI_PASS
#define AVC_WIFI_PASS CONFIG_AVC_WIFI_PASS
#else
#define AVC_WIFI_PASS "CHANGE_ME"
#endif
#ifdef CONFIG_AVC_DEST_IP
#define AVC_DEST_IP CONFIG_AVC_DEST_IP
#else
#define AVC_DEST_IP "192.168.1.100" /* gateway / PC running scripts/udp_capture.py */
#endif
#ifdef CONFIG_AVC_DEST_PORT
#define AVC_DEST_PORT CONFIG_AVC_DEST_PORT
#else
#define AVC_DEST_PORT 7777
#endif
#ifdef CONFIG_AVC_SYNTHETIC_SENSORS
#define AVC_SYNTHETIC_SENSORS 1
#else
#define AVC_SYNTHETIC_SENSORS 0
#endif

static const char *TAG = "avc";

/* ---- window buffers (BSS ~17.5 KB + packet buffer ~16.2 KB) ----------- */

static int16_t s_mic[AVC_MIC_WINDOW_MAX];
static int16_t s_piezo[AVC_PIEZO_WINDOW_MAX];
static int16_t s_pressure[AVC_PRESSURE_WINDOW_MAX];
static int16_t s_airflow[AVC_AIRFLOW_WINDOW_MAX];
static uint8_t s_pkt[AVC_PACKET_MAX_LEN];

static volatile uint16_t s_mic_n;
static volatile uint16_t s_piezo_n;
static volatile uint16_t s_pressure_n;
static volatile uint16_t s_airflow_n;
static uint32_t s_seq_no;

/* ---- WiFi + UDP ------------------------------------------------------- */

#define WIFI_CONNECTED_BIT BIT0
static EventGroupHandle_t s_wifi_events;

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi disconnected, reconnecting...");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
        err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    s_wifi_events = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_cfg_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid = AVC_WIFI_SSID,
            .password = AVC_WIFI_PASS,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "connecting to WiFi \"%s\"...", AVC_WIFI_SSID);
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT,
                                           pdTRUE, pdTRUE, portMAX_DELAY);
    ESP_LOGI(TAG, "WiFi connected (bits=%lu)", (unsigned long)bits);
}

static int s_sock = -1;
static struct sockaddr_in s_dest;

static void udp_init(void)
{
    s_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (s_sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno=%d", errno);
        return;
    }
    memset(&s_dest, 0, sizeof(s_dest));
    s_dest.sin_family = AF_INET;
    s_dest.sin_port = htons(AVC_DEST_PORT);
    if (inet_aton(AVC_DEST_IP, &s_dest.sin_addr) == 0) {
        ESP_LOGE(TAG, "bad AVC_DEST_IP \"%s\"", AVC_DEST_IP);
    }
}

static void udp_send(const uint8_t *pkt, size_t len)
{
    if (s_sock < 0) {
        return;
    }
    int sent = sendto(s_sock, pkt, len, 0,
                      (struct sockaddr *)&s_dest, sizeof(s_dest));
    if (sent < 0) {
        ESP_LOGW(TAG, "sendto() failed: errno=%d", errno);
    } else if ((size_t)sent != len) {
        ESP_LOGW(TAG, "sendto() partial: %d/%u", sent, (unsigned)len);
    }
}

/* ---- sensor drivers --------------------------------------------------- */

#if AVC_SYNTHETIC_SENSORS
/* Triangle wave at ~200 Hz on mic (period 80 samples @16 kHz), slow ramps
 * on the low-rate channels — enough to exercise segmentation + features
 * end-to-end and to eyeball plots, nothing more. */
static int32_t synth_phase;

static int16_t synth_mic_sample(void)
{
    synth_phase = (synth_phase + 1) % 80;
    int32_t tri = (synth_phase < 40) ? (synth_phase * 2 - 40)
                                     : (40 - (synth_phase - 40) * 2);
    return (int16_t)(tri * 400); /* amplitude ~0.49 full-scale */
}
static int16_t synth_piezo_sample(void)  { return 8192; }
static int16_t synth_pressure_sample(void) { return 4096; }
static int16_t synth_airflow_sample(void)  { return -4096; }
#else
/* TODO: real drivers (blocked on board/pinout decision — see README).
 *   mic:      I2S std-mode RX, 16 kHz, 16-bit mono, MCLK n/a
 *   piezo:    ADC1 @ 1 kHz, scaled to int16
 *   pressure: ADC1 @ 100 Hz (I2C sensor if the board provides one)
 *   airflow:  ADC1 @ 100 Hz
 * Each returns one int16 sample; the pacing loop below calls them at
 * their nominal rates. */
static int16_t synth_mic_sample(void)      { return 0; }
static int16_t synth_piezo_sample(void)    { return 0; }
static int16_t synth_pressure_sample(void) { return 0; }
static int16_t synth_airflow_sample(void)  { return 0; }
#endif

/* ---- acquisition + transmit task -------------------------------------- */

static void reset_window(void)
{
    s_mic_n = 0;
    s_piezo_n = 0;
    s_pressure_n = 0;
    s_airflow_n = 0;
}

/* Emit one packet for the just-completed window. */
static void emit_window(void)
{
    const avc_sensor_block_t blocks[4] = {
        {AVC_SENSOR_MIC,      s_mic,      s_mic_n},
        {AVC_SENSOR_PIEZO,    s_piezo,    s_piezo_n},
        {AVC_SENSOR_PRESSURE, s_pressure, s_pressure_n},
        {AVC_SENSOR_AIRFLOW,  s_airflow,  s_airflow_n},
    };
    size_t len = avc_packet_build(s_seq_no++,
                                  (uint32_t)(esp_timer_get_time() / 1000),
                                  blocks, 4, s_pkt, sizeof(s_pkt));
    if (len == 0) {
        ESP_LOGE(TAG, "packet build failed (window overrun?)");
        return;
    }
    udp_send(s_pkt, len);
    ESP_LOGI(TAG, "seq=%lu mic=%u piezo=%u press=%u air=%u len=%u",
             (unsigned long)(s_seq_no - 1), s_mic_n, s_piezo_n,
             s_pressure_n, s_airflow_n, (unsigned)len);
}

static void acquisition_task(void *arg)
{
    (void)arg;
    const uint32_t piezo_div = AVC_MIC_RATE_HZ / AVC_PIEZO_RATE_HZ;   /* 16 */
    const uint32_t slow_div = AVC_MIC_RATE_HZ / AVC_PRESSURE_RATE_HZ; /* 160 */
    uint32_t tick = 0;
    reset_window();

    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period = pdMS_TO_TICKS(16); /* 16 kHz / 256-sample chunk */

    for (;;) {
        /* one 16 ms chunk = 256 mic samples (synthetic; with a real I2S
         * driver this is where i2s_channel_read() goes) */
        for (int i = 0; i < 256; ++i) {
            if (s_mic_n < AVC_MIC_WINDOW_MAX) {
                s_mic[s_mic_n++] = synth_mic_sample();
            }
            if ((tick % piezo_div) == 0 && s_piezo_n < AVC_PIEZO_WINDOW_MAX) {
                s_piezo[s_piezo_n++] = synth_piezo_sample();
            }
            if ((tick % slow_div) == 0) {
                if (s_pressure_n < AVC_PRESSURE_WINDOW_MAX) {
                    s_pressure[s_pressure_n++] = synth_pressure_sample();
                }
                if (s_airflow_n < AVC_AIRFLOW_WINDOW_MAX) {
                    s_airflow[s_airflow_n++] = synth_airflow_sample();
                }
            }
            ++tick;
        }
        if (s_mic_n >= AVC_MIC_WINDOW_MAX) { /* 8000 samples = 500 ms */
            emit_window();
            reset_window();
            tick = 0;
        }
        vTaskDelayUntil(&last_wake, period);
    }
}

/* ---- entry point ------------------------------------------------------ */

void app_main(void)
{
    ESP_LOGI(TAG, "AVC firmware starting (synthetic=%d dest=%s:%u)",
             AVC_SYNTHETIC_SENSORS, AVC_DEST_IP, AVC_DEST_PORT);

    wifi_init();
    udp_init();

    if (xTaskCreatePinnedToCore(acquisition_task, "acq", 4096, NULL, 5,
                                NULL, 1) != pdPASS) {
        ESP_LOGE(TAG, "failed to create acquisition task");
    }
}
