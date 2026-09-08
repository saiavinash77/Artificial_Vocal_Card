/*
 * AVC ESP32-S3 firmware — sensor acquisition + UDP transport (skeleton).
 *
 * Architecture (TRD section 4):
 *   - One acquisition task paces everything off the 16 kHz mic clock
 *     (16 ms I2S chunks). Piezo (8 kHz, ADS126x via SPI) fires every 2nd
 *     mic sample, pressure/airflow (100 Hz) every 160th.
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
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

#include "driver/uart.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"

#include "onset.h"
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
#ifdef CONFIG_AVC_SERIAL_BAUD
#define AVC_SERIAL_BAUD CONFIG_AVC_SERIAL_BAUD
#else
#define AVC_SERIAL_BAUD 921600
#endif
#ifdef CONFIG_AVC_TRANSPORT
#define AVC_TRANSPORT CONFIG_AVC_TRANSPORT
#else
#define AVC_TRANSPORT "usb_serial"
#endif
#if defined(CONFIG_AVC_SENSOR_SET_CLOSED4)
#define AVC_SENSOR_SET_CLOSED4 1
#else
#define AVC_SENSOR_SET_CLOSED4 0
#endif
#ifdef CONFIG_AVC_ONSET_GATE
#define AVC_ONSET_GATE 1
#else
#define AVC_ONSET_GATE 0
#endif
#ifdef CONFIG_AVC_SYNTHETIC_SENSORS
#define AVC_SYNTHETIC_SENSORS 1
#else
#define AVC_SYNTHETIC_SENSORS 0
#endif

#define TRANSPORT_IS_CSV (strcmp(AVC_TRANSPORT, "usb_serial") == 0 || \
                          strcmp(AVC_TRANSPORT, "both") == 0)
#define TRANSPORT_IS_UDP (strcmp(AVC_TRANSPORT, "udp") == 0 || \
                         strcmp(AVC_TRANSPORT, "both") == 0)

static const char *TAG = "avc";

/* ---- window buffers (BSS ~24.2 KB + packet buffer ~23.7 KB) ----------- */

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

/* ---- WiFi + UDP (transport 'udp' / 'both' only) ---------------------- */

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

static int wifi_transport_needed(void)
{
    return TRANSPORT_IS_UDP;  /* strcmp resolved at compile time via -O */
}

static void udp_init(void)
{
    if (!wifi_transport_needed()) {
        return;                    /* usb_serial mode: no WiFi at all */
    }
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

/* ---- USB-serial CSV transport (default; SATHVANI doc §8 MVP) --------- */

static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* Append base64 of n little-endian int16 samples to the output buffer.
 * Returns bytes appended, or -1 when out of capacity. A full open2
 * window needs ~24.4 KB binary -> ~32.5 KB base64. */
static int b64_append_int16(char *out, size_t cap, const int16_t *samples,
                            size_t n)
{
    size_t need = ((n * 2u + 2u) / 3u) * 4u;
    if (need + 1u > cap) {
        return -1;
    }
    size_t o = 0;
    const uint8_t *b = (const uint8_t *)samples;
    size_t full = (n * 2u) / 3u;
    for (size_t g = 0; g < full; ++g) {
        uint32_t v = ((uint32_t)b[3 * g] << 16) | ((uint32_t)b[3 * g + 1] << 8) |
                     (uint32_t)b[3 * g + 2];
        out[o++] = B64[(v >> 18) & 0x3F];
        out[o++] = B64[(v >> 12) & 0x3F];
        out[o++] = B64[(v >> 6) & 0x3F];
        out[o++] = B64[v & 0x3F];
    }
    size_t rem = (n * 2u) - full * 3u;
    if (rem == 1u) {
        uint32_t v = (uint32_t)b[full * 3u] << 16;
        out[o++] = B64[(v >> 18) & 0x3F];
        out[o++] = B64[(v >> 12) & 0x3F];
        out[o++] = '=';
        out[o++] = '=';
    } else if (rem == 2u) {
        uint32_t v = ((uint32_t)b[full * 3u] << 16) |
                     ((uint32_t)b[full * 3u + 1] << 8);
        out[o++] = B64[(v >> 18) & 0x3F];
        out[o++] = B64[(v >> 12) & 0x3F];
        out[o++] = B64[(v >> 6) & 0x3F];
        out[o++] = '=';
    }
    return (int)o;
}

/* Build one AVC1 CSV line (gateway mirror: scripts/csv_logger.py
 * parse_csv_line). Layout:
 *   AVC1,<seq>,<ts_ms>,<mask_hex>,<onset>,<label>,<b64 mic>,<b64 piezo>,<crc>
 * The label is empty from the device; the logger attaches word labels.
 * Returns line length (NUL-terminated), or 0 on capacity/argument errors. */
static size_t csv_build_line(char *out, size_t cap, uint32_t seq,
                              uint32_t ts_ms, uint8_t mask, int onset,
                              const int16_t *mic, size_t mic_n,
                              const int16_t *piezo, size_t piezo_n)
{
    if (out == NULL || cap == 0u) {
        return 0u;
    }
    int hdr = snprintf(out, cap, "AVC1,%lu,%lu,%02x,%d,,",
                       (unsigned long)seq, (unsigned long)ts_ms,
                       (unsigned)mask, onset);
    /* NOTE the ',,' — onset field, then an EMPTY label field (the
     * device never labels; scripts/csv_logger.py attaches word labels).
     * Mirror of: fields=[MAGIC,seq,ts,mask,onset,label] in csv_line. */
    if (hdr <= 0 || (size_t)hdr >= cap) {
        return 0u;
    }
    size_t o = (size_t)hdr;
    if (mask & AVC_SENSOR_MIC) {
        if (mic == NULL) {
            return 0u;
        }
        int n = b64_append_int16(out + o, cap - o, mic, mic_n);
        if (n < 0) {
            return 0u;
        }
        o += (size_t)n;
    }
    if (mask & AVC_SENSOR_PIEZO) {
        if (o < cap) {
            out[o++] = ',';
        }
        if (piezo == NULL) {
            return 0u;
        }
        int n = b64_append_int16(out + o, cap - o, piezo, piezo_n);
        if (n < 0) {
            return 0u;
        }
        o += (size_t)n;
    }
    /* CRC16 over everything before the trailing comma, as hex */
    uint16_t crc = avc_crc16((const uint8_t *)out, o);
    int tail = snprintf(out + o, cap - o, ",%04x", (unsigned)crc);
    if (tail <= 0 || (size_t)tail >= cap - o) {
        return 0u;
    }
    return o + (size_t)tail;
}

/* ---- USB serial -------------------------------------------------------- */

static void serial_csv_init(void)
{
    if (!TRANSPORT_IS_CSV) {
        return;
    }
    const uart_config_t cfg = {
        .baud_rate = AVC_SERIAL_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    /* USB-UART on the ESP32-S3 DevKitC is UART0 (the console). */
    uart_param_config(UART_NUM_0, &cfg);
    uart_set_pin(UART_NUM_0, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE,
                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    uart_driver_install(UART_NUM_0, 256, 0, 0, NULL, 0);
}

/* One CSV line per window (~32.5 KB open2) + slack. */
static char s_csv[36 * 1024];

static void csv_send_window(uint32_t seq, uint32_t ts_ms, uint8_t mask,
                            int onset)
{
    size_t len = csv_build_line(s_csv, sizeof(s_csv), seq, ts_ms, mask,
                               onset,
                               s_mic, s_mic_n,
                               s_piezo, s_piezo_n);
    if (len == 0u) {
        ESP_LOGE(TAG, "csv_build_line failed (window too big?)");
        return;
    }
    s_csv[len] = '\n';
    /* One blocking write per window; 32.5 KB @ 921600 8N1 is ~353 ms,
     * which fits the 500 ms period with margin. */
    uart_write_bytes(UART_NUM_0, s_csv, len + 1);
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
 *   mic:      I2S std-mode RX, 16 kHz, 16-bit mono (ICS-43434: 24-bit
 *             data in 32-bit frame, take top 16)
 *   piezo:    SPI ADS126x @ 8 kSPS, 32-bit reads scaled to int16 (open
 *             device; pins per SATHVANI doc §4.3)
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

/* Emit one window for the just-completed 500 ms acquisition. */
static void emit_window(void)
{
    uint8_t mask = AVC_SENSOR_MIC | AVC_SENSOR_PIEZO;
    if (AVC_SENSOR_SET_CLOSED4) {
        mask |= AVC_SENSOR_PRESSURE | AVC_SENSOR_AIRFLOW;
    }

#if AVC_ONSET_GATE
    /* Firmware pre-filter (SATHVANI §5): drop silent windows early to
     * save serial bandwidth. OFF by default — the gateway's int16-scale
     * gate makes the split decision. */
    if (!avc_onset_window(s_mic, s_mic_n, AVC_MIC_RATE_HZ)) {
        ESP_LOGI(TAG, "seq=%lu dropped (silent)", (unsigned long)s_seq_no);
        s_seq_no++;
        return;
    }
#endif

    if (TRANSPORT_IS_CSV) {
        csv_send_window(s_seq_no,
                        (uint32_t)(esp_timer_get_time() / 1000),
                        mask, AVC_ONSET_GATE ? 1 : 0);
    }
    if (TRANSPORT_IS_UDP) {
        const avc_sensor_block_t blocks[4] = {
            {AVC_SENSOR_MIC,      s_mic,      s_mic_n},
            {AVC_SENSOR_PIEZO,    s_piezo,    s_piezo_n},
            {AVC_SENSOR_PRESSURE, s_pressure, s_pressure_n},
            {AVC_SENSOR_AIRFLOW,  s_airflow,  s_airflow_n},
        };
        uint8_t n_blocks = (uint8_t)(AVC_SENSOR_SET_CLOSED4 ? 4 : 2);
        size_t len = avc_packet_build(s_seq_no,
                                      (uint32_t)(esp_timer_get_time() / 1000),
                                      blocks, n_blocks, s_pkt, sizeof(s_pkt));
        if (len == 0u) {
            ESP_LOGE(TAG, "packet build failed (window overrun?)");
        } else {
            udp_send(s_pkt, len);
            ESP_LOGI(TAG, "seq=%lu mic=%u piezo=%u len=%u",
                     (unsigned long)s_seq_no, s_mic_n, s_piezo_n,
                     (unsigned)len);
        }
    }
    s_seq_no++;
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
            if (AVC_SENSOR_SET_CLOSED4) {
                if ((tick % slow_div) == 0) {
                    if (s_pressure_n < AVC_PRESSURE_WINDOW_MAX) {
                        s_pressure[s_pressure_n++] = synth_pressure_sample();
                    }
                    if (s_airflow_n < AVC_AIRFLOW_WINDOW_MAX) {
                        s_airflow[s_airflow_n++] = synth_airflow_sample();
                    }
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
    ESP_LOGI(TAG, "AVC firmware starting (synthetic=%d transport=%s set=%s)",
             AVC_SYNTHETIC_SENSORS, AVC_TRANSPORT,
             AVC_SENSOR_SET_CLOSED4 ? "closed4" : "open2");

    serial_csv_init();
    if (wifi_transport_needed()) {
        wifi_init();
        udp_init();
    }

    if (xTaskCreatePinnedToCore(acquisition_task, "acq", 4096, NULL, 5,
                                NULL, 1) != pdPASS) {
        ESP_LOGE(TAG, "failed to create acquisition task");
    }
}
