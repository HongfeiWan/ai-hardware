/*
 * AI Hardware ESP32 Fixture MCP server.
 *
 * This firmware exposes a small allowlisted MCP surface for fixture-side
 * actions: MUX selection, DUT reset, load switch control, ADC raw reads and
 * fixture status. The Python bench MCP server should remain responsible for
 * instrument control, model calls and high-risk decision making.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#if CONFIG_FIXTURE_ADC_ENABLE
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#endif

#include "esp_http_server.h"
#include "esp_mcp_data.h"
#include "esp_mcp_engine.h"
#include "esp_mcp_mgr.h"
#include "esp_mcp_property.h"
#include "esp_mcp_resource.h"
#include "esp_mcp_tool.h"

#ifndef CONFIG_FIXTURE_DIN0_ACTIVE_LOW
#define CONFIG_FIXTURE_DIN0_ACTIVE_LOW 0
#endif
#ifndef CONFIG_FIXTURE_DIN0_PULLUP
#define CONFIG_FIXTURE_DIN0_PULLUP 0
#endif
#ifndef CONFIG_FIXTURE_DIN0_PULLDOWN
#define CONFIG_FIXTURE_DIN0_PULLDOWN 0
#endif
#ifndef CONFIG_FIXTURE_DIN1_ACTIVE_LOW
#define CONFIG_FIXTURE_DIN1_ACTIVE_LOW 0
#endif
#ifndef CONFIG_FIXTURE_DIN1_PULLUP
#define CONFIG_FIXTURE_DIN1_PULLUP 0
#endif
#ifndef CONFIG_FIXTURE_DIN1_PULLDOWN
#define CONFIG_FIXTURE_DIN1_PULLDOWN 0
#endif
#ifndef CONFIG_FIXTURE_DIN2_ACTIVE_LOW
#define CONFIG_FIXTURE_DIN2_ACTIVE_LOW 0
#endif
#ifndef CONFIG_FIXTURE_DIN2_PULLUP
#define CONFIG_FIXTURE_DIN2_PULLUP 0
#endif
#ifndef CONFIG_FIXTURE_DIN2_PULLDOWN
#define CONFIG_FIXTURE_DIN2_PULLDOWN 0
#endif
#ifndef CONFIG_FIXTURE_DIN3_ACTIVE_LOW
#define CONFIG_FIXTURE_DIN3_ACTIVE_LOW 0
#endif
#ifndef CONFIG_FIXTURE_DIN3_PULLUP
#define CONFIG_FIXTURE_DIN3_PULLUP 0
#endif
#ifndef CONFIG_FIXTURE_DIN3_PULLDOWN
#define CONFIG_FIXTURE_DIN3_PULLDOWN 0
#endif

static const char *TAG = "ai_fixture";

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
#define RUNTIME_NET_MAP_SLOT_COUNT 8
#define RUNTIME_NET_LABEL_LEN      32
#define RUNTIME_NET_NVS_NAMESPACE  "fixture_net"

static char s_ip_addr[16] = "0.0.0.0";
static int s_mux_channel = 0;
static bool s_load_switch_enabled;
static char s_selected_net_label[64] = "";

typedef struct {
    const char *label;
    int channel;
} fixture_net_entry_t;

typedef struct {
    char label[RUNTIME_NET_LABEL_LEN];
    int channel;
    bool enabled;
} fixture_runtime_net_entry_t;

typedef struct {
    const char *label;
    int channel;
    const char *source;
} fixture_net_selection_t;

typedef struct {
    const char *label;
    int gpio;
    bool active_low;
    bool pullup;
    bool pulldown;
} fixture_digital_input_entry_t;

static const fixture_net_entry_t s_net_map[] = {
    {CONFIG_FIXTURE_NET0_LABEL, CONFIG_FIXTURE_NET0_CHANNEL},
    {CONFIG_FIXTURE_NET1_LABEL, CONFIG_FIXTURE_NET1_CHANNEL},
    {CONFIG_FIXTURE_NET2_LABEL, CONFIG_FIXTURE_NET2_CHANNEL},
    {CONFIG_FIXTURE_NET3_LABEL, CONFIG_FIXTURE_NET3_CHANNEL},
    {CONFIG_FIXTURE_NET4_LABEL, CONFIG_FIXTURE_NET4_CHANNEL},
    {CONFIG_FIXTURE_NET5_LABEL, CONFIG_FIXTURE_NET5_CHANNEL},
    {CONFIG_FIXTURE_NET6_LABEL, CONFIG_FIXTURE_NET6_CHANNEL},
    {CONFIG_FIXTURE_NET7_LABEL, CONFIG_FIXTURE_NET7_CHANNEL},
};

static fixture_runtime_net_entry_t s_runtime_net_map[RUNTIME_NET_MAP_SLOT_COUNT];

static const fixture_digital_input_entry_t s_digital_inputs[] = {
    {CONFIG_FIXTURE_DIN0_LABEL, CONFIG_FIXTURE_DIN0_GPIO, CONFIG_FIXTURE_DIN0_ACTIVE_LOW, CONFIG_FIXTURE_DIN0_PULLUP, CONFIG_FIXTURE_DIN0_PULLDOWN},
    {CONFIG_FIXTURE_DIN1_LABEL, CONFIG_FIXTURE_DIN1_GPIO, CONFIG_FIXTURE_DIN1_ACTIVE_LOW, CONFIG_FIXTURE_DIN1_PULLUP, CONFIG_FIXTURE_DIN1_PULLDOWN},
    {CONFIG_FIXTURE_DIN2_LABEL, CONFIG_FIXTURE_DIN2_GPIO, CONFIG_FIXTURE_DIN2_ACTIVE_LOW, CONFIG_FIXTURE_DIN2_PULLUP, CONFIG_FIXTURE_DIN2_PULLDOWN},
    {CONFIG_FIXTURE_DIN3_LABEL, CONFIG_FIXTURE_DIN3_GPIO, CONFIG_FIXTURE_DIN3_ACTIVE_LOW, CONFIG_FIXTURE_DIN3_PULLUP, CONFIG_FIXTURE_DIN3_PULLDOWN},
};

#if CONFIG_FIXTURE_WIFI_MODE_STA
static EventGroupHandle_t s_wifi_event_group;
static int s_sta_retry_count;
#endif

#if CONFIG_FIXTURE_ADC_ENABLE
static adc_oneshot_unit_handle_t s_adc_handle;
static adc_cali_handle_t s_adc_cali_handle;
static bool s_adc_ready;
static bool s_adc_cali_ready;
static char s_adc_error[64] = "not_initialized";
static char s_adc_cali_error[64] = "not_initialized";
#endif

static bool gpio_is_enabled(int gpio)
{
    return gpio >= 0;
}

static bool output_gpio_is_usable(int gpio)
{
    if (!gpio_is_enabled(gpio)) {
        return false;
    }
    if (!GPIO_IS_VALID_OUTPUT_GPIO((gpio_num_t)gpio)) {
        ESP_LOGW(TAG, "GPIO%d is not a valid output on this target; skipping it", gpio);
        return false;
    }
    return true;
}

static bool output_gpio_config_is_ok(int gpio)
{
    return !gpio_is_enabled(gpio) || GPIO_IS_VALID_OUTPUT_GPIO((gpio_num_t)gpio);
}

static bool input_gpio_config_is_ok(int gpio)
{
    return !gpio_is_enabled(gpio) || GPIO_IS_VALID_GPIO((gpio_num_t)gpio);
}

static bool gpio_conflicts_with_fixture_output(int gpio)
{
    if (!gpio_is_enabled(gpio)) {
        return false;
    }
    return gpio == CONFIG_FIXTURE_RESET_GPIO ||
           gpio == CONFIG_FIXTURE_LOAD_SWITCH_GPIO ||
           gpio == CONFIG_FIXTURE_MUX_SEL0_GPIO ||
           gpio == CONFIG_FIXTURE_MUX_SEL1_GPIO ||
           gpio == CONFIG_FIXTURE_MUX_SEL2_GPIO;
}

static int configured_adc_gpio(void)
{
#if CONFIG_FIXTURE_ADC_ENABLE
    if (CONFIG_FIXTURE_ADC_UNIT == 1) {
        const int adc1_gpios[] = {36, 37, 38, 39, 32, 33, 34, 35};
        if (CONFIG_FIXTURE_ADC_CHANNEL >= 0 &&
            CONFIG_FIXTURE_ADC_CHANNEL < (int)(sizeof(adc1_gpios) / sizeof(adc1_gpios[0]))) {
            return adc1_gpios[CONFIG_FIXTURE_ADC_CHANNEL];
        }
    } else if (CONFIG_FIXTURE_ADC_UNIT == 2) {
        const int adc2_gpios[] = {4, 0, 2, 15, 13, 12, 14, 27, 25, 26};
        if (CONFIG_FIXTURE_ADC_CHANNEL >= 0 &&
            CONFIG_FIXTURE_ADC_CHANNEL < (int)(sizeof(adc2_gpios) / sizeof(adc2_gpios[0]))) {
            return adc2_gpios[CONFIG_FIXTURE_ADC_CHANNEL];
        }
    }
#endif
    return -1;
}

static bool gpio_conflicts_with_adc(int gpio)
{
    return gpio_is_enabled(gpio) && configured_adc_gpio() == gpio;
}

static bool gpio_is_boot_strapping_pin(int gpio)
{
    switch (gpio) {
    case 0:
    case 2:
    case 4:
    case 5:
    case 12:
    case 15:
        return true;
    default:
        return false;
    }
}

static void warn_if_boot_strapping_gpio(const char *name, int gpio)
{
    if (gpio_is_enabled(gpio) && gpio_is_boot_strapping_pin(gpio)) {
        ESP_LOGW(TAG,
                 "%s uses GPIO%d, an ESP32 boot strapping pin. Ensure the fixture does not force an unsafe boot level.",
                 name,
                 gpio);
    }
}

static bool json_appendf(char *buf, size_t len, size_t *offset, const char *fmt, ...)
{
    if (*offset >= len) {
        return false;
    }

    va_list args;
    va_start(args, fmt);
    const int written = vsnprintf(buf + *offset, len - *offset, fmt, args);
    va_end(args);
    if (written < 0) {
        return false;
    }
    if ((size_t)written >= len - *offset) {
        *offset = len - 1;
        return false;
    }
    *offset += (size_t)written;
    return true;
}

static bool json_append_escaped_string(char *buf, size_t len, size_t *offset, const char *text)
{
    if (!json_appendf(buf, len, offset, "\"")) {
        return false;
    }

    for (const unsigned char *cursor = (const unsigned char *)text; cursor && *cursor; cursor++) {
        switch (*cursor) {
        case '"':
            if (!json_appendf(buf, len, offset, "\\\"")) {
                return false;
            }
            break;
        case '\\':
            if (!json_appendf(buf, len, offset, "\\\\")) {
                return false;
            }
            break;
        case '\b':
            if (!json_appendf(buf, len, offset, "\\b")) {
                return false;
            }
            break;
        case '\f':
            if (!json_appendf(buf, len, offset, "\\f")) {
                return false;
            }
            break;
        case '\n':
            if (!json_appendf(buf, len, offset, "\\n")) {
                return false;
            }
            break;
        case '\r':
            if (!json_appendf(buf, len, offset, "\\r")) {
                return false;
            }
            break;
        case '\t':
            if (!json_appendf(buf, len, offset, "\\t")) {
                return false;
            }
            break;
        default:
            if (*cursor < 0x20) {
                if (!json_appendf(buf, len, offset, "\\u%04x", *cursor)) {
                    return false;
                }
            } else if (!json_appendf(buf, len, offset, "%c", *cursor)) {
                return false;
            }
            break;
        }
    }

    return json_appendf(buf, len, offset, "\"");
}

static int count_boot_strapping_gpios(void)
{
    const int gpios[] = {
        CONFIG_FIXTURE_RESET_GPIO,
        CONFIG_FIXTURE_LOAD_SWITCH_GPIO,
        CONFIG_FIXTURE_MUX_SEL0_GPIO,
        CONFIG_FIXTURE_MUX_SEL1_GPIO,
        CONFIG_FIXTURE_MUX_SEL2_GPIO,
    };
    int count = 0;
    for (size_t i = 0; i < sizeof(gpios) / sizeof(gpios[0]); i++) {
        if (gpio_is_enabled(gpios[i]) && gpio_is_boot_strapping_pin(gpios[i])) {
            count++;
        }
    }
    return count;
}

static int count_invalid_output_gpios(void)
{
    const int gpios[] = {
        CONFIG_FIXTURE_RESET_GPIO,
        CONFIG_FIXTURE_LOAD_SWITCH_GPIO,
        CONFIG_FIXTURE_MUX_SEL0_GPIO,
        CONFIG_FIXTURE_MUX_SEL1_GPIO,
        CONFIG_FIXTURE_MUX_SEL2_GPIO,
    };
    int count = 0;
    for (size_t i = 0; i < sizeof(gpios) / sizeof(gpios[0]); i++) {
        if (!output_gpio_config_is_ok(gpios[i])) {
            count++;
        }
    }
    return count;
}

static int mux_required_select_bits(void)
{
    int max_channel = CONFIG_FIXTURE_MUX_MAX_CHANNEL;
    int bits = 0;
    while (max_channel > 0) {
        bits++;
        max_channel >>= 1;
    }
    return bits;
}

static bool mux_config_supports_max_channel(void)
{
    const int mux_gpios[] = {
        CONFIG_FIXTURE_MUX_SEL0_GPIO,
        CONFIG_FIXTURE_MUX_SEL1_GPIO,
        CONFIG_FIXTURE_MUX_SEL2_GPIO,
    };
    const int required_bits = mux_required_select_bits();
    if (required_bits > (int)(sizeof(mux_gpios) / sizeof(mux_gpios[0]))) {
        return false;
    }
    for (int bit = 0; bit < required_bits; bit++) {
        if (!gpio_is_enabled(mux_gpios[bit]) || !output_gpio_config_is_ok(mux_gpios[bit])) {
            return false;
        }
    }
    return true;
}

static bool mux_channel_can_be_selected(int channel)
{
    const int mux_gpios[] = {
        CONFIG_FIXTURE_MUX_SEL0_GPIO,
        CONFIG_FIXTURE_MUX_SEL1_GPIO,
        CONFIG_FIXTURE_MUX_SEL2_GPIO,
    };
    for (size_t bit = 0; bit < sizeof(mux_gpios) / sizeof(mux_gpios[0]); bit++) {
        if (((channel >> bit) & 0x1) &&
            (!gpio_is_enabled(mux_gpios[bit]) || !output_gpio_config_is_ok(mux_gpios[bit]))) {
            return false;
        }
    }
    return true;
}

static bool net_label_is_enabled(const char *label)
{
    return label && label[0] != '\0';
}

static bool net_selection_is_selectable(const fixture_net_selection_t *entry)
{
    return entry &&
           net_label_is_enabled(entry->label) &&
           entry->channel >= 0 &&
           entry->channel <= CONFIG_FIXTURE_MUX_MAX_CHANNEL &&
           mux_channel_can_be_selected(entry->channel);
}

static bool net_entry_is_selectable(const fixture_net_entry_t *entry)
{
    const fixture_net_selection_t selection = {
        .label = entry->label,
        .channel = entry->channel,
        .source = "default",
    };
    return net_selection_is_selectable(&selection);
}

static bool runtime_net_entry_is_enabled(const fixture_runtime_net_entry_t *entry)
{
    return entry && entry->enabled && net_label_is_enabled(entry->label);
}

static bool runtime_net_label_shadows_default(const char *label)
{
    if (!net_label_is_enabled(label)) {
        return false;
    }
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (runtime_net_entry_is_enabled(&s_runtime_net_map[i]) &&
            strcmp(s_runtime_net_map[i].label, label) == 0) {
            return true;
        }
    }
    return false;
}

static bool runtime_net_entry_is_selectable(const fixture_runtime_net_entry_t *entry)
{
    const fixture_net_selection_t selection = {
        .label = entry->label,
        .channel = entry->channel,
        .source = "runtime",
    };
    return runtime_net_entry_is_enabled(entry) && net_selection_is_selectable(&selection);
}

static int count_enabled_net_entries(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (runtime_net_entry_is_enabled(&s_runtime_net_map[i])) {
            count++;
        }
    }
    for (size_t i = 0; i < sizeof(s_net_map) / sizeof(s_net_map[0]); i++) {
        if (net_label_is_enabled(s_net_map[i].label) &&
            !runtime_net_label_shadows_default(s_net_map[i].label)) {
            count++;
        }
    }
    return count;
}

static int count_invalid_net_entries(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (runtime_net_entry_is_enabled(&s_runtime_net_map[i]) &&
            !runtime_net_entry_is_selectable(&s_runtime_net_map[i])) {
            count++;
        }
    }
    for (size_t i = 0; i < sizeof(s_net_map) / sizeof(s_net_map[0]); i++) {
        if (net_label_is_enabled(s_net_map[i].label) &&
            !runtime_net_label_shadows_default(s_net_map[i].label) &&
            !net_entry_is_selectable(&s_net_map[i])) {
            count++;
        }
    }
    return count;
}

static int count_runtime_net_entries(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (runtime_net_entry_is_enabled(&s_runtime_net_map[i])) {
            count++;
        }
    }
    return count;
}

static bool find_net_selection(const char *label, fixture_net_selection_t *selection)
{
    if (!net_label_is_enabled(label) || !selection) {
        return false;
    }

    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (runtime_net_entry_is_enabled(&s_runtime_net_map[i]) &&
            strcmp(s_runtime_net_map[i].label, label) == 0) {
            selection->label = s_runtime_net_map[i].label;
            selection->channel = s_runtime_net_map[i].channel;
            selection->source = "runtime";
            return true;
        }
    }

    for (size_t i = 0; i < sizeof(s_net_map) / sizeof(s_net_map[0]); i++) {
        if (net_label_is_enabled(s_net_map[i].label) && strcmp(s_net_map[i].label, label) == 0) {
            selection->label = s_net_map[i].label;
            selection->channel = s_net_map[i].channel;
            selection->source = "default";
            return true;
        }
    }
    return false;
}

static void runtime_net_key(char *buf, size_t len, const char *prefix, size_t index)
{
    snprintf(buf, len, "%s%u", prefix, (unsigned int)index);
}

static int find_runtime_net_slot_by_label(const char *label)
{
    if (!net_label_is_enabled(label)) {
        return -1;
    }
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (runtime_net_entry_is_enabled(&s_runtime_net_map[i]) &&
            strcmp(s_runtime_net_map[i].label, label) == 0) {
            return (int)i;
        }
    }
    return -1;
}

static int find_runtime_net_free_slot(void)
{
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (!runtime_net_entry_is_enabled(&s_runtime_net_map[i])) {
            return (int)i;
        }
    }
    return -1;
}

static void clear_runtime_net_slot(size_t index)
{
    if (index >= sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0])) {
        return;
    }
    s_runtime_net_map[index].label[0] = '\0';
    s_runtime_net_map[index].channel = 0;
    s_runtime_net_map[index].enabled = false;
}

static esp_err_t erase_runtime_net_slot_from_nvs(size_t index)
{
    nvs_handle_t handle;
    esp_err_t ret = nvs_open(RUNTIME_NET_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (ret != ESP_OK) {
        return ret;
    }
    char key[12];
    runtime_net_key(key, sizeof(key), "label", index);
    esp_err_t label_ret = nvs_erase_key(handle, key);
    runtime_net_key(key, sizeof(key), "chan", index);
    esp_err_t channel_ret = nvs_erase_key(handle, key);
    ret = nvs_commit(handle);
    nvs_close(handle);
    if (label_ret != ESP_OK && label_ret != ESP_ERR_NVS_NOT_FOUND) {
        return label_ret;
    }
    if (channel_ret != ESP_OK && channel_ret != ESP_ERR_NVS_NOT_FOUND) {
        return channel_ret;
    }
    return ret;
}

static esp_err_t save_runtime_net_slot_to_nvs(size_t index)
{
    if (index >= sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]) ||
        !runtime_net_entry_is_enabled(&s_runtime_net_map[index])) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t handle;
    esp_err_t ret = nvs_open(RUNTIME_NET_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (ret != ESP_OK) {
        return ret;
    }

    char key[12];
    runtime_net_key(key, sizeof(key), "label", index);
    ret = nvs_set_str(handle, key, s_runtime_net_map[index].label);
    if (ret == ESP_OK) {
        runtime_net_key(key, sizeof(key), "chan", index);
        ret = nvs_set_i32(handle, key, s_runtime_net_map[index].channel);
    }
    if (ret == ESP_OK) {
        ret = nvs_commit(handle);
    }
    nvs_close(handle);
    return ret;
}

static void load_runtime_net_map_from_nvs(void)
{
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        clear_runtime_net_slot(i);
    }

    nvs_handle_t handle;
    esp_err_t ret = nvs_open(RUNTIME_NET_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (ret == ESP_ERR_NVS_NOT_FOUND) {
        return;
    }
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Failed to open runtime net map NVS: %s", esp_err_to_name(ret));
        return;
    }

    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        char key[12];
        char label[RUNTIME_NET_LABEL_LEN] = {0};
        size_t label_len = sizeof(label);
        runtime_net_key(key, sizeof(key), "label", i);
        ret = nvs_get_str(handle, key, label, &label_len);
        if (ret == ESP_ERR_NVS_NOT_FOUND) {
            continue;
        }
        if (ret != ESP_OK || !net_label_is_enabled(label)) {
            ESP_LOGW(TAG, "Ignoring invalid runtime net label in slot %u", (unsigned int)i);
            continue;
        }
        int32_t channel = 0;
        runtime_net_key(key, sizeof(key), "chan", i);
        ret = nvs_get_i32(handle, key, &channel);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Ignoring runtime net %s with missing channel", label);
            continue;
        }
        snprintf(s_runtime_net_map[i].label, sizeof(s_runtime_net_map[i].label), "%s", label);
        s_runtime_net_map[i].channel = channel;
        s_runtime_net_map[i].enabled = true;
    }
    nvs_close(handle);
}

static bool digital_input_entry_is_enabled(const fixture_digital_input_entry_t *entry)
{
    return net_label_is_enabled(entry->label) && gpio_is_enabled(entry->gpio);
}

static bool digital_input_entry_is_valid(const fixture_digital_input_entry_t *entry)
{
    return digital_input_entry_is_enabled(entry) &&
           input_gpio_config_is_ok(entry->gpio) &&
           !gpio_conflicts_with_fixture_output(entry->gpio) &&
           !gpio_conflicts_with_adc(entry->gpio) &&
           !(entry->pullup && entry->pulldown);
}

static bool digital_input_gpio_is_duplicate(size_t index)
{
    if (index >= sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]) ||
        !digital_input_entry_is_enabled(&s_digital_inputs[index])) {
        return false;
    }

    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (i != index &&
            digital_input_entry_is_enabled(&s_digital_inputs[i]) &&
            s_digital_inputs[i].gpio == s_digital_inputs[index].gpio) {
            return true;
        }
    }
    return false;
}

static bool digital_input_label_is_duplicate(size_t index)
{
    if (index >= sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]) ||
        !digital_input_entry_is_enabled(&s_digital_inputs[index])) {
        return false;
    }

    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (i != index &&
            digital_input_entry_is_enabled(&s_digital_inputs[i]) &&
            strcmp(s_digital_inputs[i].label, s_digital_inputs[index].label) == 0) {
            return true;
        }
    }
    return false;
}

static bool digital_input_entry_at_index_is_valid(size_t index)
{
    if (index >= sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0])) {
        return false;
    }
    return digital_input_entry_is_valid(&s_digital_inputs[index]) &&
           !digital_input_gpio_is_duplicate(index) &&
           !digital_input_label_is_duplicate(index);
}

static int count_enabled_digital_inputs(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (digital_input_entry_is_enabled(&s_digital_inputs[i])) {
            count++;
        }
    }
    return count;
}

static int count_invalid_digital_inputs(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (digital_input_entry_is_enabled(&s_digital_inputs[i]) &&
            !digital_input_entry_at_index_is_valid(i)) {
            count++;
        }
    }
    return count;
}

static int count_adc_conflicting_digital_inputs(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (digital_input_entry_is_enabled(&s_digital_inputs[i]) &&
            gpio_conflicts_with_adc(s_digital_inputs[i].gpio)) {
            count++;
        }
    }
    return count;
}

static int count_duplicate_digital_input_gpios(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (digital_input_gpio_is_duplicate(i)) {
            count++;
        }
    }
    return count;
}

static int count_duplicate_digital_input_labels(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (digital_input_label_is_duplicate(i)) {
            count++;
        }
    }
    return count;
}

static int count_boot_strapping_digital_inputs(void)
{
    int count = 0;
    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (digital_input_entry_is_enabled(&s_digital_inputs[i]) &&
            gpio_is_boot_strapping_pin(s_digital_inputs[i].gpio)) {
            count++;
        }
    }
    return count;
}

static const fixture_digital_input_entry_t *find_digital_input_entry(const char *label)
{
    if (!net_label_is_enabled(label)) {
        return NULL;
    }
    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (digital_input_entry_is_enabled(&s_digital_inputs[i]) &&
            strcmp(s_digital_inputs[i].label, label) == 0) {
            return &s_digital_inputs[i];
        }
    }
    return NULL;
}

static void configure_output_gpio(int gpio, int initial_level)
{
    if (!output_gpio_is_usable(gpio)) {
        return;
    }

    gpio_config_t io_conf = {
        .pin_bit_mask = 1ULL << gpio,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io_conf));
    ESP_ERROR_CHECK(gpio_set_level((gpio_num_t)gpio, initial_level));
}

static void configure_input_gpio(size_t index)
{
    if (index >= sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0])) {
        return;
    }
    const fixture_digital_input_entry_t *entry = &s_digital_inputs[index];
    if (!digital_input_entry_is_enabled(entry)) {
        return;
    }
    if (!digital_input_entry_at_index_is_valid(index)) {
        ESP_LOGW(TAG, "Digital input %s on GPIO%d is not valid; skipping it", entry->label, entry->gpio);
        return;
    }

    gpio_config_t io_conf = {
        .pin_bit_mask = 1ULL << entry->gpio,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = entry->pullup ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE,
        .pull_down_en = entry->pulldown ? GPIO_PULLDOWN_ENABLE : GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io_conf));
}

static bool digital_input_active_state(const fixture_digital_input_entry_t *entry, int raw_level)
{
    return entry->active_low ? raw_level == 0 : raw_level != 0;
}

static void set_reset_inactive(void)
{
    if (!output_gpio_is_usable(CONFIG_FIXTURE_RESET_GPIO)) {
        return;
    }
    const int inactive_level = CONFIG_FIXTURE_RESET_ACTIVE_LOW ? 1 : 0;
    ESP_ERROR_CHECK(gpio_set_level((gpio_num_t)CONFIG_FIXTURE_RESET_GPIO, inactive_level));
}

static void set_reset_active(void)
{
    if (!output_gpio_is_usable(CONFIG_FIXTURE_RESET_GPIO)) {
        return;
    }
    const int active_level = CONFIG_FIXTURE_RESET_ACTIVE_LOW ? 0 : 1;
    ESP_ERROR_CHECK(gpio_set_level((gpio_num_t)CONFIG_FIXTURE_RESET_GPIO, active_level));
}

static void set_load_switch(bool enabled)
{
    if (!output_gpio_is_usable(CONFIG_FIXTURE_LOAD_SWITCH_GPIO)) {
        s_load_switch_enabled = false;
        return;
    }

    const bool active_high = CONFIG_FIXTURE_LOAD_SWITCH_ACTIVE_HIGH;
    const int level = enabled ? (active_high ? 1 : 0) : (active_high ? 0 : 1);
    ESP_ERROR_CHECK(gpio_set_level((gpio_num_t)CONFIG_FIXTURE_LOAD_SWITCH_GPIO, level));
    s_load_switch_enabled = enabled;
}

static void set_mux_channel(int channel)
{
    const int mux_gpios[] = {
        CONFIG_FIXTURE_MUX_SEL0_GPIO,
        CONFIG_FIXTURE_MUX_SEL1_GPIO,
        CONFIG_FIXTURE_MUX_SEL2_GPIO,
    };

    for (size_t bit = 0; bit < sizeof(mux_gpios) / sizeof(mux_gpios[0]); bit++) {
        if (output_gpio_is_usable(mux_gpios[bit])) {
            ESP_ERROR_CHECK(gpio_set_level((gpio_num_t)mux_gpios[bit], (channel >> bit) & 0x1));
        }
    }
    s_mux_channel = channel;
}

static esp_mcp_value_t json_value(const char *json)
{
    esp_mcp_value_t value = esp_mcp_value_create_string(json);
    if (value.type == ESP_MCP_VALUE_TYPE_INVALID) {
        return esp_mcp_value_create_bool(false);
    }
    return value;
}

static void append_status_json(char *buf, size_t len)
{
    char adc_json[640] = {0};
    char selected_net_json[96] = {0};
    size_t selected_net_offset = 0;
    if (s_selected_net_label[0]) {
        json_append_escaped_string(selected_net_json, sizeof(selected_net_json), &selected_net_offset, s_selected_net_label);
    } else {
        json_appendf(selected_net_json, sizeof(selected_net_json), &selected_net_offset, "null");
    }
#if CONFIG_FIXTURE_ADC_ENABLE
    snprintf(adc_json, sizeof(adc_json),
             ",\"adc_enabled\":true,"
             "\"adc_ready\":%s,"
             "\"adc_error\":\"%s\","
             "\"adc_calibration_enabled\":%s,"
             "\"adc_calibration_ready\":%s,"
             "\"adc_calibration_error\":\"%s\","
             "\"adc_default_vref_mv\":%d,"
             "\"adc_scale_numerator\":%d,"
             "\"adc_scale_denominator\":%d,"
             "\"adc_offset_mv\":%d,"
             "\"adc_unit\":%d,"
             "\"adc_channel\":%d,"
             "\"adc_series_max_points\":%d,"
             "\"adc_series_max_samples_per_point\":%d,"
             "\"adc_series_max_interval_ms\":%d,"
             "\"adc_series_max_total_ms\":%d",
             s_adc_ready ? "true" : "false",
             s_adc_ready ? "" : s_adc_error,
             CONFIG_FIXTURE_ADC_CALIBRATION_ENABLE ? "true" : "false",
             s_adc_cali_ready ? "true" : "false",
             s_adc_cali_ready ? "" : s_adc_cali_error,
             CONFIG_FIXTURE_ADC_DEFAULT_VREF_MV,
             CONFIG_FIXTURE_ADC_SCALE_NUMERATOR,
             CONFIG_FIXTURE_ADC_SCALE_DENOMINATOR,
             CONFIG_FIXTURE_ADC_OFFSET_MV,
             CONFIG_FIXTURE_ADC_UNIT,
             CONFIG_FIXTURE_ADC_CHANNEL,
             CONFIG_FIXTURE_ADC_SERIES_MAX_POINTS,
             CONFIG_FIXTURE_ADC_SERIES_MAX_SAMPLES_PER_POINT,
             CONFIG_FIXTURE_ADC_SERIES_MAX_INTERVAL_MS,
             CONFIG_FIXTURE_ADC_SERIES_MAX_TOTAL_MS);
#else
    snprintf(adc_json, sizeof(adc_json), ",\"adc_enabled\":false,\"adc_ready\":false");
#endif

    snprintf(buf, len,
             "{\"ok\":true,"
             "\"device\":\"ai-hardware-esp32-fixture\","
             "\"endpoint\":\"/%s\","
             "\"ip\":\"%s\","
             "\"uptime_ms\":%lld,"
             "\"mux_channel\":%d,"
             "\"selected_net\":%s,"
             "\"load_switch_enabled\":%s,"
             "\"reset_gpio\":%d,"
             "\"load_switch_gpio\":%d,"
             "\"mux_gpios\":[%d,%d,%d],"
             "\"runtime_net_entry_count\":%d,"
             "\"digital_input_count\":%d,"
             "\"invalid_digital_input_count\":%d"
             "%s}",
             CONFIG_FIXTURE_MCP_ENDPOINT,
             s_ip_addr,
             (long long)(esp_timer_get_time() / 1000),
             s_mux_channel,
             selected_net_json,
             s_load_switch_enabled ? "true" : "false",
             CONFIG_FIXTURE_RESET_GPIO,
             CONFIG_FIXTURE_LOAD_SWITCH_GPIO,
             CONFIG_FIXTURE_MUX_SEL0_GPIO,
             CONFIG_FIXTURE_MUX_SEL1_GPIO,
             CONFIG_FIXTURE_MUX_SEL2_GPIO,
             count_runtime_net_entries(),
             count_enabled_digital_inputs(),
             count_invalid_digital_inputs(),
             adc_json);
}

static void append_net_map_json(char *buf, size_t len)
{
    const int entry_count = count_enabled_net_entries();
    const int invalid_entry_count = count_invalid_net_entries();
    int runtime_entry_count = 0;
    size_t offset = 0;
    bool first = true;
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (runtime_net_entry_is_enabled(&s_runtime_net_map[i])) {
            runtime_entry_count++;
        }
    }

    json_appendf(buf,
                 len,
                 &offset,
                 "{\"ok\":%s,"
                 "\"entry_count\":%d,"
                 "\"runtime_entry_count\":%d,"
                 "\"invalid_entry_count\":%d,"
                 "\"mux_max_channel\":%d,"
                 "\"entries\":[",
                 invalid_entry_count == 0 ? "true" : "false",
                 entry_count,
                 runtime_entry_count,
                 invalid_entry_count,
                 CONFIG_FIXTURE_MUX_MAX_CHANNEL);

    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (!runtime_net_entry_is_enabled(&s_runtime_net_map[i])) {
            continue;
        }

        if (!first) {
            json_appendf(buf, len, &offset, ",");
        }
        first = false;
        json_appendf(buf, len, &offset, "{\"net\":");
        json_append_escaped_string(buf, len, &offset, s_runtime_net_map[i].label);
        json_appendf(buf,
                     len,
                     &offset,
                     ",\"mux_channel\":%d,\"source\":\"runtime\",\"slot\":%u,\"selectable\":%s}",
                     s_runtime_net_map[i].channel,
                     (unsigned int)i,
                     runtime_net_entry_is_selectable(&s_runtime_net_map[i]) ? "true" : "false");
    }

    for (size_t i = 0; i < sizeof(s_net_map) / sizeof(s_net_map[0]); i++) {
        if (!net_label_is_enabled(s_net_map[i].label) ||
            runtime_net_label_shadows_default(s_net_map[i].label)) {
            continue;
        }

        if (!first) {
            json_appendf(buf, len, &offset, ",");
        }
        first = false;
        json_appendf(buf, len, &offset, "{\"net\":");
        json_append_escaped_string(buf, len, &offset, s_net_map[i].label);
        json_appendf(buf,
                     len,
                     &offset,
                     ",\"mux_channel\":%d,\"source\":\"default\",\"selectable\":%s}",
                     s_net_map[i].channel,
                     net_entry_is_selectable(&s_net_map[i]) ? "true" : "false");
    }

    json_appendf(buf, len, &offset, "]}");
}

static void append_digital_inputs_json(char *buf, size_t len)
{
    const int entry_count = count_enabled_digital_inputs();
    const int invalid_entry_count = count_invalid_digital_inputs();
    size_t offset = 0;
    bool first = true;

    json_appendf(buf,
                 len,
                 &offset,
                 "{\"ok\":%s,"
                 "\"entry_count\":%d,"
                 "\"invalid_entry_count\":%d,"
                 "\"entries\":[",
                 invalid_entry_count == 0 ? "true" : "false",
                 entry_count,
                 invalid_entry_count);

    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        const fixture_digital_input_entry_t *entry = &s_digital_inputs[i];
        if (!digital_input_entry_is_enabled(entry)) {
            continue;
        }
        if (!first) {
            json_appendf(buf, len, &offset, ",");
        }
        first = false;
        json_appendf(buf, len, &offset, "{\"label\":");
        json_append_escaped_string(buf, len, &offset, entry->label);
        json_appendf(buf,
                     len,
                     &offset,
                     ",\"gpio\":%d,"
                     "\"active_low\":%s,"
                     "\"pullup\":%s,"
                     "\"pulldown\":%s,"
                     "\"conflicts_with_output\":%s,"
                     "\"conflicts_with_adc\":%s,"
                     "\"duplicate_gpio\":%s,"
                     "\"duplicate_label\":%s,"
                     "\"valid\":%s}",
                     entry->gpio,
                     entry->active_low ? "true" : "false",
                     entry->pullup ? "true" : "false",
                     entry->pulldown ? "true" : "false",
                     gpio_conflicts_with_fixture_output(entry->gpio) ? "true" : "false",
                     gpio_conflicts_with_adc(entry->gpio) ? "true" : "false",
                     digital_input_gpio_is_duplicate(i) ? "true" : "false",
                     digital_input_label_is_duplicate(i) ? "true" : "false",
                     digital_input_entry_at_index_is_valid(i) ? "true" : "false");
    }

    json_appendf(buf, len, &offset, "]}");
}

static esp_mcp_value_t ping_callback(const esp_mcp_property_list_t *properties)
{
    (void)properties;
    char payload[192];
    snprintf(payload, sizeof(payload),
             "{\"ok\":true,\"device\":\"ai-hardware-esp32-fixture\",\"uptime_ms\":%lld}",
             (long long)(esp_timer_get_time() / 1000));
    return json_value(payload);
}

static esp_mcp_value_t get_status_callback(const esp_mcp_property_list_t *properties)
{
    (void)properties;
    char payload[1280];
    append_status_json(payload, sizeof(payload));
    return json_value(payload);
}

static esp_mcp_value_t self_test_callback(const esp_mcp_property_list_t *properties)
{
    (void)properties;

    const int invalid_output_gpios = count_invalid_output_gpios();
    const int boot_strapping_gpios = count_boot_strapping_gpios();
    const int required_mux_bits = mux_required_select_bits();
    const bool mux_config_ok = mux_config_supports_max_channel();
    const bool mux_settle_ok = CONFIG_FIXTURE_MUX_SETTLE_MS <= CONFIG_FIXTURE_MAX_MUX_SETTLE_MS;
    const int net_map_entry_count = count_enabled_net_entries();
    const int runtime_net_map_entry_count = count_runtime_net_entries();
    const int invalid_net_map_entries = count_invalid_net_entries();
    const int digital_input_count = count_enabled_digital_inputs();
    const int invalid_digital_inputs = count_invalid_digital_inputs();
    const int boot_strapping_digital_inputs = count_boot_strapping_digital_inputs();
    const int adc_conflicting_digital_inputs = count_adc_conflicting_digital_inputs();
    const int duplicate_digital_input_gpios = count_duplicate_digital_input_gpios();
    const int duplicate_digital_input_labels = count_duplicate_digital_input_labels();
#if CONFIG_FIXTURE_ADC_ENABLE
    const bool adc_ok = s_adc_ready;
    const bool adc_scale_ok = CONFIG_FIXTURE_ADC_SCALE_DENOMINATOR > 0;
    const bool adc_series_limits_ok = CONFIG_FIXTURE_ADC_SERIES_MAX_POINTS >= 4 &&
                                      CONFIG_FIXTURE_ADC_SERIES_MAX_SAMPLES_PER_POINT >= 4 &&
                                      CONFIG_FIXTURE_ADC_SERIES_MAX_INTERVAL_MS >= 20 &&
                                      CONFIG_FIXTURE_ADC_SERIES_MAX_TOTAL_MS >= 100;
#else
    const bool adc_ok = true;
    const bool adc_scale_ok = true;
    const bool adc_series_limits_ok = true;
#endif
    const bool ok = invalid_output_gpios == 0 &&
                    mux_config_ok &&
                    mux_settle_ok &&
                    invalid_net_map_entries == 0 &&
                    invalid_digital_inputs == 0 &&
                    adc_ok &&
                    adc_scale_ok &&
                    adc_series_limits_ok;

    char payload[2560];
    snprintf(payload, sizeof(payload),
             "{\"ok\":%s,"
             "\"invalid_output_gpio_count\":%d,"
             "\"boot_strapping_gpio_count\":%d,"
             "\"boot_strapping_digital_input_count\":%d,"
             "\"reset_gpio_ok\":%s,"
             "\"load_switch_gpio_ok\":%s,"
             "\"mux_max_channel\":%d,"
             "\"mux_required_select_bits\":%d,"
             "\"mux_settle_ms\":%d,"
             "\"max_mux_settle_ms\":%d,"
             "\"mux_settle_ok\":%s,"
             "\"mux_config_ok\":%s,"
             "\"mux_gpio_ok\":[%s,%s,%s],"
             "\"net_map_entry_count\":%d,"
             "\"runtime_net_map_entry_count\":%d,"
             "\"invalid_net_map_entry_count\":%d,"
             "\"net_map_ok\":%s,"
             "\"digital_input_count\":%d,"
             "\"invalid_digital_input_count\":%d,"
             "\"adc_conflicting_digital_input_count\":%d,"
             "\"duplicate_digital_input_gpio_count\":%d,"
             "\"duplicate_digital_input_label_count\":%d,"
             "\"digital_inputs_ok\":%s,"
             "\"free_heap_bytes\":%u,"
             "\"minimum_free_heap_bytes\":%u,"
             "\"adc_enabled\":%s,"
             "\"adc_ready\":%s,"
             "\"adc_calibration_enabled\":%s,"
             "\"adc_calibration_ready\":%s,"
             "\"adc_default_vref_mv\":%d,"
             "\"adc_scale_numerator\":%d,"
             "\"adc_scale_denominator\":%d,"
             "\"adc_offset_mv\":%d,"
             "\"adc_scale_ok\":%s,"
             "\"adc_series_max_points\":%d,"
             "\"adc_series_max_samples_per_point\":%d,"
             "\"adc_series_max_interval_ms\":%d,"
             "\"adc_series_max_total_ms\":%d,"
             "\"adc_series_limits_ok\":%s"
#if CONFIG_FIXTURE_ADC_ENABLE
             ",\"adc_error\":\"%s\","
             "\"adc_calibration_error\":\"%s\""
#endif
             "}",
             ok ? "true" : "false",
             invalid_output_gpios,
             boot_strapping_gpios,
             boot_strapping_digital_inputs,
             output_gpio_config_is_ok(CONFIG_FIXTURE_RESET_GPIO) ? "true" : "false",
             output_gpio_config_is_ok(CONFIG_FIXTURE_LOAD_SWITCH_GPIO) ? "true" : "false",
             CONFIG_FIXTURE_MUX_MAX_CHANNEL,
             required_mux_bits,
             CONFIG_FIXTURE_MUX_SETTLE_MS,
             CONFIG_FIXTURE_MAX_MUX_SETTLE_MS,
             mux_settle_ok ? "true" : "false",
             mux_config_ok ? "true" : "false",
             output_gpio_config_is_ok(CONFIG_FIXTURE_MUX_SEL0_GPIO) ? "true" : "false",
             output_gpio_config_is_ok(CONFIG_FIXTURE_MUX_SEL1_GPIO) ? "true" : "false",
             output_gpio_config_is_ok(CONFIG_FIXTURE_MUX_SEL2_GPIO) ? "true" : "false",
             net_map_entry_count,
             runtime_net_map_entry_count,
             invalid_net_map_entries,
             invalid_net_map_entries == 0 ? "true" : "false",
             digital_input_count,
             invalid_digital_inputs,
             adc_conflicting_digital_inputs,
             duplicate_digital_input_gpios,
             duplicate_digital_input_labels,
             invalid_digital_inputs == 0 ? "true" : "false",
             (unsigned int)esp_get_free_heap_size(),
             (unsigned int)esp_get_minimum_free_heap_size(),
#if CONFIG_FIXTURE_ADC_ENABLE
             "true",
             s_adc_ready ? "true" : "false",
             CONFIG_FIXTURE_ADC_CALIBRATION_ENABLE ? "true" : "false",
             s_adc_cali_ready ? "true" : "false",
             CONFIG_FIXTURE_ADC_DEFAULT_VREF_MV,
             CONFIG_FIXTURE_ADC_SCALE_NUMERATOR,
             CONFIG_FIXTURE_ADC_SCALE_DENOMINATOR,
             CONFIG_FIXTURE_ADC_OFFSET_MV,
             adc_scale_ok ? "true" : "false",
             CONFIG_FIXTURE_ADC_SERIES_MAX_POINTS,
             CONFIG_FIXTURE_ADC_SERIES_MAX_SAMPLES_PER_POINT,
             CONFIG_FIXTURE_ADC_SERIES_MAX_INTERVAL_MS,
             CONFIG_FIXTURE_ADC_SERIES_MAX_TOTAL_MS,
             adc_series_limits_ok ? "true" : "false",
             s_adc_ready ? "" : s_adc_error,
             s_adc_cali_ready ? "" : s_adc_cali_error
#else
             "false",
             "false",
             "false",
             "false",
             0,
             1,
             1,
             0,
             "true",
             0,
             0,
             0,
             0,
             "true"
#endif
            );
    return json_value(payload);
}

static esp_mcp_value_t set_mux_channel_callback(const esp_mcp_property_list_t *properties)
{
    const int channel = esp_mcp_property_list_get_property_int(properties, "channel");
    if (channel < 0 || channel > CONFIG_FIXTURE_MUX_MAX_CHANNEL) {
        char payload[128];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"channel_out_of_range\",\"max_channel\":%d}",
                 CONFIG_FIXTURE_MUX_MAX_CHANNEL);
        return json_value(payload);
    }
    if (!mux_channel_can_be_selected(channel)) {
        char payload[160];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"mux_channel_not_representable\",\"channel\":%d}",
                 channel);
        return json_value(payload);
    }

    set_mux_channel(channel);
    s_selected_net_label[0] = '\0';

    char payload[128];
    snprintf(payload, sizeof(payload), "{\"ok\":true,\"mux_channel\":%d}", s_mux_channel);
    return json_value(payload);
}

static esp_mcp_value_t select_net_callback(const esp_mcp_property_list_t *properties)
{
    const char *net = esp_mcp_property_list_get_property_string(properties, "net");
    if (!net_label_is_enabled(net)) {
        return json_value("{\"ok\":false,\"error\":\"missing_net\"}");
    }

    fixture_net_selection_t entry = {0};
    if (!find_net_selection(net, &entry)) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload, sizeof(payload), &offset, "{\"ok\":false,\"error\":\"unknown_net\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, net);
        json_appendf(payload, sizeof(payload), &offset, "}");
        return json_value(payload);
    }

    if (entry.channel < 0 || entry.channel > CONFIG_FIXTURE_MUX_MAX_CHANNEL) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     "{\"ok\":false,\"error\":\"net_channel_out_of_range\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, entry.label);
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     ",\"mux_channel\":%d,\"max_channel\":%d}",
                     entry.channel,
                     CONFIG_FIXTURE_MUX_MAX_CHANNEL);
        return json_value(payload);
    }

    if (!mux_channel_can_be_selected(entry.channel)) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     "{\"ok\":false,\"error\":\"net_channel_not_representable\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, entry.label);
        json_appendf(payload, sizeof(payload), &offset, ",\"mux_channel\":%d}", entry.channel);
        return json_value(payload);
    }

    set_mux_channel(entry.channel);
    snprintf(s_selected_net_label, sizeof(s_selected_net_label), "%s", entry.label);

    char payload[256];
    size_t offset = 0;
    json_appendf(payload, sizeof(payload), &offset, "{\"ok\":true,\"net\":");
    json_append_escaped_string(payload, sizeof(payload), &offset, entry.label);
    json_appendf(payload, sizeof(payload), &offset, ",\"mux_channel\":%d,\"source\":\"%s\"}", s_mux_channel, entry.source);
    return json_value(payload);
}

static esp_mcp_value_t set_runtime_net_callback(const esp_mcp_property_list_t *properties)
{
    const char *net = esp_mcp_property_list_get_property_string(properties, "net");
    if (!net_label_is_enabled(net)) {
        return json_value("{\"ok\":false,\"error\":\"missing_net\"}");
    }
    if (strlen(net) >= RUNTIME_NET_LABEL_LEN) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"net_label_too_long\",\"max_length\":%d}",
                 RUNTIME_NET_LABEL_LEN - 1);
        return json_value(payload);
    }

    const int channel = esp_mcp_property_list_get_property_int(properties, "channel");
    if (channel < 0 || channel > CONFIG_FIXTURE_MUX_MAX_CHANNEL) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"channel_out_of_range\",\"max_channel\":%d}",
                 CONFIG_FIXTURE_MUX_MAX_CHANNEL);
        return json_value(payload);
    }
    if (!mux_channel_can_be_selected(channel)) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"mux_channel_not_representable\",\"channel\":%d}",
                 channel);
        return json_value(payload);
    }

    int slot = find_runtime_net_slot_by_label(net);
    if (slot < 0) {
        slot = find_runtime_net_free_slot();
    }
    if (slot < 0) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"runtime_net_map_full\",\"max_entries\":%d}",
                 RUNTIME_NET_MAP_SLOT_COUNT);
        return json_value(payload);
    }

    snprintf(s_runtime_net_map[slot].label, sizeof(s_runtime_net_map[slot].label), "%s", net);
    s_runtime_net_map[slot].channel = channel;
    s_runtime_net_map[slot].enabled = true;

    esp_err_t ret = save_runtime_net_slot_to_nvs((size_t)slot);
    if (ret != ESP_OK) {
        char payload[192];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"nvs_save_failed\",\"esp_err\":\"%s\"}",
                 esp_err_to_name(ret));
        return json_value(payload);
    }

    char payload[256];
    size_t offset = 0;
    json_appendf(payload, sizeof(payload), &offset, "{\"ok\":true,\"net\":");
    json_append_escaped_string(payload, sizeof(payload), &offset, net);
    json_appendf(payload,
                 sizeof(payload),
                 &offset,
                 ",\"mux_channel\":%d,\"slot\":%d,\"persisted\":true}",
                 channel,
                 slot);
    return json_value(payload);
}

static esp_mcp_value_t clear_runtime_net_callback(const esp_mcp_property_list_t *properties)
{
    const char *net = esp_mcp_property_list_get_property_string(properties, "net");
    if (!net_label_is_enabled(net)) {
        return json_value("{\"ok\":false,\"error\":\"missing_net\"}");
    }

    const int slot = find_runtime_net_slot_by_label(net);
    if (slot < 0) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload, sizeof(payload), &offset, "{\"ok\":false,\"error\":\"runtime_net_not_found\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, net);
        json_appendf(payload, sizeof(payload), &offset, "}");
        return json_value(payload);
    }

    esp_err_t ret = erase_runtime_net_slot_from_nvs((size_t)slot);
    clear_runtime_net_slot((size_t)slot);
    if (ret != ESP_OK) {
        char payload[192];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"nvs_erase_failed\",\"esp_err\":\"%s\"}",
                 esp_err_to_name(ret));
        return json_value(payload);
    }

    char payload[256];
    size_t offset = 0;
    json_appendf(payload, sizeof(payload), &offset, "{\"ok\":true,\"net\":");
    json_append_escaped_string(payload, sizeof(payload), &offset, net);
    json_appendf(payload, sizeof(payload), &offset, ",\"slot\":%d}", slot);
    return json_value(payload);
}

static esp_mcp_value_t clear_runtime_net_map_callback(const esp_mcp_property_list_t *properties)
{
    (void)properties;
    int cleared = 0;
    esp_err_t first_error = ESP_OK;
    for (size_t i = 0; i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (!runtime_net_entry_is_enabled(&s_runtime_net_map[i])) {
            continue;
        }
        esp_err_t ret = erase_runtime_net_slot_from_nvs(i);
        if (ret != ESP_OK && first_error == ESP_OK) {
            first_error = ret;
        }
        clear_runtime_net_slot(i);
        cleared++;
    }

    if (first_error != ESP_OK) {
        char payload[192];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"nvs_erase_failed\",\"esp_err\":\"%s\",\"cleared_count\":%d}",
                 esp_err_to_name(first_error),
                 cleared);
        return json_value(payload);
    }

    char payload[128];
    snprintf(payload, sizeof(payload), "{\"ok\":true,\"cleared_count\":%d}", cleared);
    return json_value(payload);
}

static esp_mcp_value_t reset_dut_callback(const esp_mcp_property_list_t *properties)
{
    if (!output_gpio_is_usable(CONFIG_FIXTURE_RESET_GPIO)) {
        return json_value("{\"ok\":false,\"error\":\"reset_gpio_disabled\"}");
    }

    int pulse_ms = esp_mcp_property_list_get_property_int(properties, "pulse_ms");
    if (pulse_ms < 10) {
        pulse_ms = 10;
    }
    if (pulse_ms > CONFIG_FIXTURE_MAX_RESET_PULSE_MS) {
        char payload[160];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"pulse_too_long\",\"max_pulse_ms\":%d}",
                 CONFIG_FIXTURE_MAX_RESET_PULSE_MS);
        return json_value(payload);
    }

    set_reset_active();
    vTaskDelay(pdMS_TO_TICKS(pulse_ms));
    set_reset_inactive();

    char payload[128];
    snprintf(payload, sizeof(payload), "{\"ok\":true,\"pulse_ms\":%d}", pulse_ms);
    return json_value(payload);
}

static esp_mcp_value_t set_load_switch_callback(const esp_mcp_property_list_t *properties)
{
    if (!output_gpio_is_usable(CONFIG_FIXTURE_LOAD_SWITCH_GPIO)) {
        return json_value("{\"ok\":false,\"error\":\"load_switch_gpio_disabled\"}");
    }

    const bool enabled = esp_mcp_property_list_get_property_bool(properties, "enabled");
    set_load_switch(enabled);

    char payload[128];
    snprintf(payload, sizeof(payload), "{\"ok\":true,\"load_switch_enabled\":%s}",
             s_load_switch_enabled ? "true" : "false");
    return json_value(payload);
}

static esp_mcp_value_t read_digital_input_callback(const esp_mcp_property_list_t *properties)
{
    const char *label = esp_mcp_property_list_get_property_string(properties, "label");
    if (!net_label_is_enabled(label)) {
        return json_value("{\"ok\":false,\"error\":\"missing_label\"}");
    }

    const fixture_digital_input_entry_t *entry = find_digital_input_entry(label);
    if (!entry) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload, sizeof(payload), &offset, "{\"ok\":false,\"error\":\"unknown_digital_input\",\"label\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, label);
        json_appendf(payload, sizeof(payload), &offset, "}");
        return json_value(payload);
    }
    const size_t index = (size_t)(entry - s_digital_inputs);
    if (!digital_input_entry_at_index_is_valid(index)) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload, sizeof(payload), &offset, "{\"ok\":false,\"error\":\"invalid_digital_input\",\"label\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, entry->label);
        json_appendf(payload, sizeof(payload), &offset, ",\"gpio\":%d}", entry->gpio);
        return json_value(payload);
    }

    const int raw_level = gpio_get_level((gpio_num_t)entry->gpio);
    char payload[256];
    size_t offset = 0;
    json_appendf(payload, sizeof(payload), &offset, "{\"ok\":true,\"label\":");
    json_append_escaped_string(payload, sizeof(payload), &offset, entry->label);
    json_appendf(payload,
                 sizeof(payload),
                 &offset,
                 ",\"gpio\":%d,\"raw_level\":%d,\"active\":%s,\"active_low\":%s}",
                 entry->gpio,
                 raw_level,
                 digital_input_active_state(entry, raw_level) ? "true" : "false",
                 entry->active_low ? "true" : "false");
    return json_value(payload);
}

static esp_mcp_value_t scan_digital_inputs_callback(const esp_mcp_property_list_t *properties)
{
    (void)properties;
    const int invalid_entry_count = count_invalid_digital_inputs();
    if (invalid_entry_count > 0) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"digital_input_map_invalid\",\"invalid_entry_count\":%d}",
                 invalid_entry_count);
        return json_value(payload);
    }

    char payload[3072];
    size_t offset = 0;
    bool first = true;
    int scanned_count = 0;
    json_appendf(payload,
                 sizeof(payload),
                 &offset,
                 "{\"ok\":true,\"entry_count\":%d,\"readings\":[",
                 count_enabled_digital_inputs());

    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        const fixture_digital_input_entry_t *entry = &s_digital_inputs[i];
        if (!digital_input_entry_is_enabled(entry)) {
            continue;
        }
        const int raw_level = gpio_get_level((gpio_num_t)entry->gpio);
        if (!first) {
            json_appendf(payload, sizeof(payload), &offset, ",");
        }
        first = false;
        scanned_count++;
        json_appendf(payload, sizeof(payload), &offset, "{\"label\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, entry->label);
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     ",\"gpio\":%d,\"raw_level\":%d,\"active\":%s,\"active_low\":%s}",
                     entry->gpio,
                     raw_level,
                     digital_input_active_state(entry, raw_level) ? "true" : "false",
                     entry->active_low ? "true" : "false");
    }

    json_appendf(payload, sizeof(payload), &offset, "],\"scanned_count\":%d}", scanned_count);
    return json_value(payload);
}

#if CONFIG_FIXTURE_ADC_ENABLE
typedef struct {
    int samples;
    int raw_avg;
    int raw_min;
    int raw_max;
    int raw_last;
    bool millivolts_valid;
    int mv_avg;
    int mv_min;
    int mv_max;
    int mv_last;
    bool scaled_millivolts_valid;
    int scaled_mv_avg;
    int scaled_mv_min;
    int scaled_mv_max;
    int scaled_mv_last;
} adc_reading_t;

static int normalize_adc_samples(int samples)
{
    if (samples < 1) {
        return 1;
    }
    if (samples > 64) {
        return 64;
    }
    return samples;
}

static int scale_adc_millivolts(int mv)
{
    const int64_t scaled = ((int64_t)mv * CONFIG_FIXTURE_ADC_SCALE_NUMERATOR) /
                           CONFIG_FIXTURE_ADC_SCALE_DENOMINATOR +
                           CONFIG_FIXTURE_ADC_OFFSET_MV;
    if (scaled > INT32_MAX) {
        return INT32_MAX;
    }
    if (scaled < INT32_MIN) {
        return INT32_MIN;
    }
    return (int)scaled;
}

static esp_err_t read_adc_average(int samples, adc_reading_t *reading)
{
    if (!reading) {
        return ESP_ERR_INVALID_ARG;
    }

    samples = normalize_adc_samples(samples);
    int sum = 0;
    int raw = 0;
    int min_raw = 0;
    int max_raw = 0;

    for (int i = 0; i < samples; i++) {
        esp_err_t ret = adc_oneshot_read(s_adc_handle, (adc_channel_t)CONFIG_FIXTURE_ADC_CHANNEL, &raw);
        if (ret != ESP_OK) {
            return ret;
        }
        if (i == 0 || raw < min_raw) {
            min_raw = raw;
        }
        if (i == 0 || raw > max_raw) {
            max_raw = raw;
        }
        sum += raw;
    }

    reading->samples = samples;
    reading->raw_avg = sum / samples;
    reading->raw_min = min_raw;
    reading->raw_max = max_raw;
    reading->raw_last = raw;
    reading->millivolts_valid = false;
    if (s_adc_cali_ready) {
        esp_err_t ret = adc_cali_raw_to_voltage(s_adc_cali_handle, reading->raw_avg, &reading->mv_avg);
        ret |= adc_cali_raw_to_voltage(s_adc_cali_handle, reading->raw_min, &reading->mv_min);
        ret |= adc_cali_raw_to_voltage(s_adc_cali_handle, reading->raw_max, &reading->mv_max);
        ret |= adc_cali_raw_to_voltage(s_adc_cali_handle, reading->raw_last, &reading->mv_last);
        reading->millivolts_valid = ret == ESP_OK;
        if (reading->millivolts_valid) {
            reading->scaled_mv_avg = scale_adc_millivolts(reading->mv_avg);
            reading->scaled_mv_min = scale_adc_millivolts(reading->mv_min);
            reading->scaled_mv_max = scale_adc_millivolts(reading->mv_max);
            reading->scaled_mv_last = scale_adc_millivolts(reading->mv_last);
            reading->scaled_millivolts_valid = true;
        }
    }
    return ESP_OK;
}

static bool append_adc_reading_json_fields(char *payload, size_t len, size_t *offset, const adc_reading_t *reading)
{
    bool ok = json_appendf(payload,
                           len,
                           offset,
                           "\"samples\":%d,"
                           "\"raw_avg\":%d,"
                           "\"raw_min\":%d,"
                           "\"raw_max\":%d,"
                           "\"raw_last\":%d,"
                           "\"millivolts_valid\":%s,"
                           "\"scaled_millivolts_valid\":%s",
                           reading->samples,
                           reading->raw_avg,
                           reading->raw_min,
                           reading->raw_max,
                           reading->raw_last,
                           reading->millivolts_valid ? "true" : "false",
                           reading->scaled_millivolts_valid ? "true" : "false");
    if (ok && reading->millivolts_valid) {
        ok = json_appendf(payload,
                          len,
                          offset,
                          ",\"mv_avg\":%d,\"mv_min\":%d,\"mv_max\":%d,\"mv_last\":%d",
                          reading->mv_avg,
                          reading->mv_min,
                          reading->mv_max,
                          reading->mv_last);
    }
    if (ok && reading->scaled_millivolts_valid) {
        ok = json_appendf(payload,
                          len,
                          offset,
                          ",\"scaled_mv_avg\":%d,\"scaled_mv_min\":%d,\"scaled_mv_max\":%d,\"scaled_mv_last\":%d",
                          reading->scaled_mv_avg,
                          reading->scaled_mv_min,
                          reading->scaled_mv_max,
                          reading->scaled_mv_last);
    }
    return ok;
}

static esp_mcp_value_t read_adc_raw_callback(const esp_mcp_property_list_t *properties)
{
    if (!s_adc_ready) {
        char payload[160];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"adc_not_ready\",\"detail\":\"%s\"}",
                 s_adc_error);
        return json_value(payload);
    }

    adc_reading_t reading = {0};
    esp_err_t ret = read_adc_average(esp_mcp_property_list_get_property_int(properties, "samples"), &reading);
    if (ret != ESP_OK) {
        char payload[192];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"adc_read_failed\",\"esp_err\":\"%s\"}",
                 esp_err_to_name(ret));
        return json_value(payload);
    }

    char payload[768];
    size_t offset = 0;
    json_appendf(payload,
                 sizeof(payload),
                 &offset,
                 "{\"ok\":true,"
                 "\"adc_unit\":%d,"
                 "\"adc_channel\":%d,"
                 "\"samples\":%d,"
                 "\"raw_avg\":%d,"
                 "\"raw_min\":%d,"
                 "\"raw_max\":%d,"
                 "\"raw_last\":%d,"
                 "\"millivolts_valid\":%s,"
                 "\"scaled_millivolts_valid\":%s",
                 CONFIG_FIXTURE_ADC_UNIT,
                 CONFIG_FIXTURE_ADC_CHANNEL,
                 reading.samples,
                 reading.raw_avg,
                 reading.raw_min,
                 reading.raw_max,
                 reading.raw_last,
                 reading.millivolts_valid ? "true" : "false",
                 reading.scaled_millivolts_valid ? "true" : "false");
    if (reading.millivolts_valid) {
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     ",\"mv_avg\":%d,\"mv_min\":%d,\"mv_max\":%d,\"mv_last\":%d",
                     reading.mv_avg,
                     reading.mv_min,
                     reading.mv_max,
                     reading.mv_last);
    }
    if (reading.scaled_millivolts_valid) {
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     ",\"scaled_mv_avg\":%d,\"scaled_mv_min\":%d,\"scaled_mv_max\":%d,\"scaled_mv_last\":%d",
                     reading.scaled_mv_avg,
                     reading.scaled_mv_min,
                     reading.scaled_mv_max,
                     reading.scaled_mv_last);
    }
    json_appendf(payload, sizeof(payload), &offset, "}");
    return json_value(payload);
}

static esp_mcp_value_t read_net_adc_raw_callback(const esp_mcp_property_list_t *properties)
{
    if (!s_adc_ready) {
        char payload[160];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"adc_not_ready\",\"detail\":\"%s\"}",
                 s_adc_error);
        return json_value(payload);
    }

    const char *net = esp_mcp_property_list_get_property_string(properties, "net");
    if (!net_label_is_enabled(net)) {
        return json_value("{\"ok\":false,\"error\":\"missing_net\"}");
    }

    fixture_net_selection_t entry = {0};
    if (!find_net_selection(net, &entry)) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload, sizeof(payload), &offset, "{\"ok\":false,\"error\":\"unknown_net\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, net);
        json_appendf(payload, sizeof(payload), &offset, "}");
        return json_value(payload);
    }

    if (entry.channel < 0 || entry.channel > CONFIG_FIXTURE_MUX_MAX_CHANNEL) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     "{\"ok\":false,\"error\":\"net_channel_out_of_range\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, entry.label);
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     ",\"mux_channel\":%d,\"max_channel\":%d}",
                     entry.channel,
                     CONFIG_FIXTURE_MUX_MAX_CHANNEL);
        return json_value(payload);
    }

    if (!mux_channel_can_be_selected(entry.channel)) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     "{\"ok\":false,\"error\":\"net_channel_not_representable\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, entry.label);
        json_appendf(payload, sizeof(payload), &offset, ",\"mux_channel\":%d}", entry.channel);
        return json_value(payload);
    }

    int settle_ms = esp_mcp_property_list_get_property_int(properties, "settle_ms");
    if (settle_ms <= 0) {
        settle_ms = CONFIG_FIXTURE_MUX_SETTLE_MS;
    }
    if (settle_ms > CONFIG_FIXTURE_MAX_MUX_SETTLE_MS) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"settle_too_long\",\"max_settle_ms\":%d}",
                 CONFIG_FIXTURE_MAX_MUX_SETTLE_MS);
        return json_value(payload);
    }

    set_mux_channel(entry.channel);
    snprintf(s_selected_net_label, sizeof(s_selected_net_label), "%s", entry.label);
    if (settle_ms > 0) {
        vTaskDelay(pdMS_TO_TICKS(settle_ms));
    }

    adc_reading_t reading = {0};
    esp_err_t ret = read_adc_average(esp_mcp_property_list_get_property_int(properties, "samples"), &reading);
    if (ret != ESP_OK) {
        char payload[192];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"adc_read_failed\",\"esp_err\":\"%s\"}",
                 esp_err_to_name(ret));
        return json_value(payload);
    }

    char payload[768];
    size_t offset = 0;
    json_appendf(payload, sizeof(payload), &offset, "{\"ok\":true,\"net\":");
    json_append_escaped_string(payload, sizeof(payload), &offset, entry.label);
    json_appendf(payload,
                 sizeof(payload),
                 &offset,
                 ",\"mux_channel\":%d,"
                 "\"source\":\"%s\","
                 "\"settle_ms\":%d,"
                 "\"adc_unit\":%d,"
                 "\"adc_channel\":%d,"
                 "\"samples\":%d,"
                 "\"raw_avg\":%d,"
                 "\"raw_min\":%d,"
                 "\"raw_max\":%d,"
                 "\"raw_last\":%d,"
                 "\"millivolts_valid\":%s,"
                 "\"scaled_millivolts_valid\":%s",
                 s_mux_channel,
                 entry.source,
                 settle_ms,
                 CONFIG_FIXTURE_ADC_UNIT,
                 CONFIG_FIXTURE_ADC_CHANNEL,
                 reading.samples,
                 reading.raw_avg,
                 reading.raw_min,
                 reading.raw_max,
                 reading.raw_last,
                 reading.millivolts_valid ? "true" : "false",
                 reading.scaled_millivolts_valid ? "true" : "false");
    if (reading.millivolts_valid) {
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     ",\"mv_avg\":%d,\"mv_min\":%d,\"mv_max\":%d,\"mv_last\":%d",
                     reading.mv_avg,
                     reading.mv_min,
                     reading.mv_max,
                     reading.mv_last);
    }
    if (reading.scaled_millivolts_valid) {
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     ",\"scaled_mv_avg\":%d,\"scaled_mv_min\":%d,\"scaled_mv_max\":%d,\"scaled_mv_last\":%d",
                     reading.scaled_mv_avg,
                     reading.scaled_mv_min,
                     reading.scaled_mv_max,
                     reading.scaled_mv_last);
    }
    json_appendf(payload, sizeof(payload), &offset, "}");
    return json_value(payload);
}

static esp_mcp_value_t sample_net_adc_series_callback(const esp_mcp_property_list_t *properties)
{
    if (!s_adc_ready) {
        char payload[160];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"adc_not_ready\",\"detail\":\"%s\"}",
                 s_adc_error);
        return json_value(payload);
    }

    const char *net = esp_mcp_property_list_get_property_string(properties, "net");
    if (!net_label_is_enabled(net)) {
        return json_value("{\"ok\":false,\"error\":\"missing_net\"}");
    }

    fixture_net_selection_t entry = {0};
    if (!find_net_selection(net, &entry)) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload, sizeof(payload), &offset, "{\"ok\":false,\"error\":\"unknown_net\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, net);
        json_appendf(payload, sizeof(payload), &offset, "}");
        return json_value(payload);
    }

    if (entry.channel < 0 || entry.channel > CONFIG_FIXTURE_MUX_MAX_CHANNEL) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     "{\"ok\":false,\"error\":\"net_channel_out_of_range\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, entry.label);
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     ",\"mux_channel\":%d,\"max_channel\":%d}",
                     entry.channel,
                     CONFIG_FIXTURE_MUX_MAX_CHANNEL);
        return json_value(payload);
    }

    if (!mux_channel_can_be_selected(entry.channel)) {
        char payload[256];
        size_t offset = 0;
        json_appendf(payload,
                     sizeof(payload),
                     &offset,
                     "{\"ok\":false,\"error\":\"net_channel_not_representable\",\"net\":");
        json_append_escaped_string(payload, sizeof(payload), &offset, entry.label);
        json_appendf(payload, sizeof(payload), &offset, ",\"mux_channel\":%d}", entry.channel);
        return json_value(payload);
    }

    int settle_ms = esp_mcp_property_list_get_property_int(properties, "settle_ms");
    if (settle_ms <= 0) {
        settle_ms = CONFIG_FIXTURE_MUX_SETTLE_MS;
    }
    if (settle_ms > CONFIG_FIXTURE_MAX_MUX_SETTLE_MS) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"settle_too_long\",\"max_settle_ms\":%d}",
                 CONFIG_FIXTURE_MAX_MUX_SETTLE_MS);
        return json_value(payload);
    }

    const int points = esp_mcp_property_list_get_property_int(properties, "points");
    if (points < 1 || points > CONFIG_FIXTURE_ADC_SERIES_MAX_POINTS) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"points_out_of_range\",\"max_points\":%d}",
                 CONFIG_FIXTURE_ADC_SERIES_MAX_POINTS);
        return json_value(payload);
    }

    const int samples_per_point = esp_mcp_property_list_get_property_int(properties, "samples_per_point");
    if (samples_per_point < 1 || samples_per_point > CONFIG_FIXTURE_ADC_SERIES_MAX_SAMPLES_PER_POINT) {
        char payload[192];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"samples_per_point_out_of_range\",\"max_samples_per_point\":%d}",
                 CONFIG_FIXTURE_ADC_SERIES_MAX_SAMPLES_PER_POINT);
        return json_value(payload);
    }

    const int interval_ms = esp_mcp_property_list_get_property_int(properties, "interval_ms");
    if (interval_ms < 0 || interval_ms > CONFIG_FIXTURE_ADC_SERIES_MAX_INTERVAL_MS) {
        char payload[192];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"interval_out_of_range\",\"max_interval_ms\":%d}",
                 CONFIG_FIXTURE_ADC_SERIES_MAX_INTERVAL_MS);
        return json_value(payload);
    }

    const int64_t requested_wait_ms = points > 1 ? (int64_t)(points - 1) * interval_ms : 0;
    if (requested_wait_ms > CONFIG_FIXTURE_ADC_SERIES_MAX_TOTAL_MS) {
        char payload[192];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"series_too_long\",\"max_total_ms\":%d}",
                 CONFIG_FIXTURE_ADC_SERIES_MAX_TOTAL_MS);
        return json_value(payload);
    }

    set_mux_channel(entry.channel);
    snprintf(s_selected_net_label, sizeof(s_selected_net_label), "%s", entry.label);
    if (settle_ms > 0) {
        vTaskDelay(pdMS_TO_TICKS(settle_ms));
    }

    char *payload = calloc(1, 6144);
    if (!payload) {
        return json_value("{\"ok\":false,\"error\":\"out_of_memory\"}");
    }

    const size_t payload_len = 6144;
    size_t offset = 0;
    bool json_ok = true;
    const int64_t start_ms = esp_timer_get_time() / 1000;

    json_ok = json_ok && json_appendf(payload, payload_len, &offset, "{\"ok\":true,\"net\":");
    json_ok = json_ok && json_append_escaped_string(payload, payload_len, &offset, entry.label);
    json_ok = json_ok && json_appendf(payload,
                                      payload_len,
                                      &offset,
                                      ",\"mux_channel\":%d,"
                                      "\"source\":\"%s\","
                                      "\"settle_ms\":%d,"
                                      "\"adc_unit\":%d,"
                                      "\"adc_channel\":%d,"
                                      "\"points\":%d,"
                                      "\"samples_per_point\":%d,"
                                      "\"interval_ms\":%d,"
                                      "\"max_total_ms\":%d,"
                                      "\"readings\":[",
                                      s_mux_channel,
                                      entry.source,
                                      settle_ms,
                                      CONFIG_FIXTURE_ADC_UNIT,
                                      CONFIG_FIXTURE_ADC_CHANNEL,
                                      points,
                                      samples_per_point,
                                      interval_ms,
                                      CONFIG_FIXTURE_ADC_SERIES_MAX_TOTAL_MS);

    for (int i = 0; json_ok && i < points; i++) {
        adc_reading_t reading = {0};
        esp_err_t ret = read_adc_average(samples_per_point, &reading);
        if (ret != ESP_OK) {
            free(payload);
            char error_payload[192];
            snprintf(error_payload,
                     sizeof(error_payload),
                     "{\"ok\":false,\"error\":\"adc_read_failed\",\"esp_err\":\"%s\",\"point_index\":%d}",
                     esp_err_to_name(ret),
                     i);
            return json_value(error_payload);
        }

        if (i > 0) {
            json_ok = json_ok && json_appendf(payload, payload_len, &offset, ",");
        }
        json_ok = json_ok && json_appendf(payload,
                                          payload_len,
                                          &offset,
                                          "{\"index\":%d,\"t_ms\":%lld,",
                                          i,
                                          (long long)((esp_timer_get_time() / 1000) - start_ms));
        json_ok = json_ok && append_adc_reading_json_fields(payload, payload_len, &offset, &reading);
        json_ok = json_ok && json_appendf(payload, payload_len, &offset, "}");

        if (i + 1 < points && interval_ms > 0) {
            vTaskDelay(pdMS_TO_TICKS(interval_ms));
        }
    }

    json_ok = json_ok && json_appendf(payload,
                                      payload_len,
                                      &offset,
                                      "],\"elapsed_ms\":%lld}",
                                      (long long)((esp_timer_get_time() / 1000) - start_ms));

    if (!json_ok) {
        free(payload);
        return json_value("{\"ok\":false,\"error\":\"response_too_large\"}");
    }

    esp_mcp_value_t value = json_value(payload);
    free(payload);
    return value;
}

static esp_err_t append_net_adc_scan_reading(char *payload,
                                             size_t payload_len,
                                             size_t *offset,
                                             const fixture_net_selection_t *entry,
                                             int settle_ms,
                                             int samples,
                                             int64_t start_ms,
                                             bool *first,
                                             int *scanned_count)
{
    if (!net_selection_is_selectable(entry)) {
        return ESP_ERR_INVALID_ARG;
    }

    set_mux_channel(entry->channel);
    snprintf(s_selected_net_label, sizeof(s_selected_net_label), "%s", entry->label);
    if (settle_ms > 0) {
        vTaskDelay(pdMS_TO_TICKS(settle_ms));
    }

    adc_reading_t reading = {0};
    esp_err_t ret = read_adc_average(samples, &reading);
    if (ret != ESP_OK) {
        return ret;
    }

    bool json_ok = true;
    if (!*first) {
        json_ok = json_ok && json_appendf(payload, payload_len, offset, ",");
    }
    *first = false;
    (*scanned_count)++;
    json_ok = json_ok && json_appendf(payload, payload_len, offset, "{\"net\":");
    json_ok = json_ok && json_append_escaped_string(payload, payload_len, offset, entry->label);
    json_ok = json_ok && json_appendf(payload,
                                      payload_len,
                                      offset,
                                      ",\"mux_channel\":%d,"
                                      "\"source\":\"%s\","
                                      "\"t_ms\":%lld,",
                                      entry->channel,
                                      entry->source,
                                      (long long)((esp_timer_get_time() / 1000) - start_ms));
    json_ok = json_ok && append_adc_reading_json_fields(payload, payload_len, offset, &reading);
    json_ok = json_ok && json_appendf(payload, payload_len, offset, "}");
    return json_ok ? ESP_OK : ESP_ERR_NO_MEM;
}

static esp_mcp_value_t scan_net_adc_callback(const esp_mcp_property_list_t *properties)
{
    if (!s_adc_ready) {
        char payload[160];
        snprintf(payload, sizeof(payload),
                 "{\"ok\":false,\"error\":\"adc_not_ready\",\"detail\":\"%s\"}",
                 s_adc_error);
        return json_value(payload);
    }

    const int invalid_net_entries = count_invalid_net_entries();
    if (invalid_net_entries > 0) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"net_map_invalid\",\"invalid_entry_count\":%d}",
                 invalid_net_entries);
        return json_value(payload);
    }

    const int enabled_net_entries = count_enabled_net_entries();
    if (enabled_net_entries <= 0) {
        return json_value("{\"ok\":false,\"error\":\"net_map_empty\"}");
    }

    int samples = esp_mcp_property_list_get_property_int(properties, "samples");
    if (samples <= 0) {
        samples = 8;
    }
    if (samples > 64) {
        return json_value("{\"ok\":false,\"error\":\"samples_out_of_range\",\"max_samples\":64}");
    }

    int settle_ms = esp_mcp_property_list_get_property_int(properties, "settle_ms");
    if (settle_ms <= 0) {
        settle_ms = CONFIG_FIXTURE_MUX_SETTLE_MS;
    }
    if (settle_ms > CONFIG_FIXTURE_MAX_MUX_SETTLE_MS) {
        char payload[160];
        snprintf(payload,
                 sizeof(payload),
                 "{\"ok\":false,\"error\":\"settle_too_long\",\"max_settle_ms\":%d}",
                 CONFIG_FIXTURE_MAX_MUX_SETTLE_MS);
        return json_value(payload);
    }

    char *payload = calloc(1, 6144);
    if (!payload) {
        return json_value("{\"ok\":false,\"error\":\"out_of_memory\"}");
    }

    const size_t payload_len = 6144;
    size_t offset = 0;
    bool json_ok = true;
    bool first = true;
    int scanned_count = 0;
    const int64_t start_ms = esp_timer_get_time() / 1000;

    json_ok = json_ok && json_appendf(payload,
                                      payload_len,
                                      &offset,
                                      "{\"ok\":true,"
                                      "\"entry_count\":%d,"
                                      "\"settle_ms\":%d,"
                                      "\"adc_unit\":%d,"
                                      "\"adc_channel\":%d,"
                                      "\"samples\":%d,"
                                      "\"readings\":[",
                                      enabled_net_entries,
                                      settle_ms,
                                      CONFIG_FIXTURE_ADC_UNIT,
                                      CONFIG_FIXTURE_ADC_CHANNEL,
                                      samples);

    for (size_t i = 0; json_ok && i < sizeof(s_runtime_net_map) / sizeof(s_runtime_net_map[0]); i++) {
        if (!runtime_net_entry_is_enabled(&s_runtime_net_map[i])) {
            continue;
        }
        const fixture_net_selection_t entry = {
            .label = s_runtime_net_map[i].label,
            .channel = s_runtime_net_map[i].channel,
            .source = "runtime",
        };
        esp_err_t ret = append_net_adc_scan_reading(payload,
                                                    payload_len,
                                                    &offset,
                                                    &entry,
                                                    settle_ms,
                                                    samples,
                                                    start_ms,
                                                    &first,
                                                    &scanned_count);
        if (ret != ESP_OK) {
            free(payload);
            char error_payload[224];
            snprintf(error_payload,
                     sizeof(error_payload),
                     "{\"ok\":false,\"error\":\"net_scan_failed\",\"esp_err\":\"%s\",\"source\":\"runtime\",\"slot\":%u}",
                     esp_err_to_name(ret),
                     (unsigned int)i);
            return json_value(error_payload);
        }
    }

    for (size_t i = 0; json_ok && i < sizeof(s_net_map) / sizeof(s_net_map[0]); i++) {
        if (!net_label_is_enabled(s_net_map[i].label) ||
            runtime_net_label_shadows_default(s_net_map[i].label)) {
            continue;
        }
        const fixture_net_selection_t entry = {
            .label = s_net_map[i].label,
            .channel = s_net_map[i].channel,
            .source = "default",
        };
        esp_err_t ret = append_net_adc_scan_reading(payload,
                                                    payload_len,
                                                    &offset,
                                                    &entry,
                                                    settle_ms,
                                                    samples,
                                                    start_ms,
                                                    &first,
                                                    &scanned_count);
        if (ret != ESP_OK) {
            free(payload);
            char error_payload[224];
            snprintf(error_payload,
                     sizeof(error_payload),
                     "{\"ok\":false,\"error\":\"net_scan_failed\",\"esp_err\":\"%s\",\"source\":\"default\",\"index\":%u}",
                     esp_err_to_name(ret),
                     (unsigned int)i);
            return json_value(error_payload);
        }
    }

    json_ok = json_ok && json_appendf(payload,
                                      payload_len,
                                      &offset,
                                      "],\"scanned_count\":%d,\"elapsed_ms\":%lld}",
                                      scanned_count,
                                      (long long)((esp_timer_get_time() / 1000) - start_ms));

    if (!json_ok) {
        free(payload);
        return json_value("{\"ok\":false,\"error\":\"response_too_large\"}");
    }

    esp_mcp_value_t value = json_value(payload);
    free(payload);
    return value;
}
#endif

static esp_err_t read_status_resource(const char *uri,
                                      char **out_mime,
                                      char **out_text,
                                      char **out_blob,
                                      void *ctx)
{
    (void)uri;
    (void)ctx;
    ESP_RETURN_ON_FALSE(out_mime && out_text && out_blob, ESP_ERR_INVALID_ARG, TAG, "Invalid resource output");

    *out_mime = strdup("application/json");
    *out_blob = NULL;
    if (!*out_mime) {
        return ESP_ERR_NO_MEM;
    }

    char payload[1280];
    append_status_json(payload, sizeof(payload));
    *out_text = strdup(payload);
    if (!*out_text) {
        free(*out_mime);
        *out_mime = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

static esp_err_t read_net_map_resource(const char *uri,
                                       char **out_mime,
                                       char **out_text,
                                       char **out_blob,
                                       void *ctx)
{
    (void)uri;
    (void)ctx;
    ESP_RETURN_ON_FALSE(out_mime && out_text && out_blob, ESP_ERR_INVALID_ARG, TAG, "Invalid resource output");

    *out_mime = strdup("application/json");
    *out_blob = NULL;
    if (!*out_mime) {
        return ESP_ERR_NO_MEM;
    }

    char payload[1536];
    append_net_map_json(payload, sizeof(payload));
    *out_text = strdup(payload);
    if (!*out_text) {
        free(*out_mime);
        *out_mime = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

static esp_err_t read_digital_inputs_resource(const char *uri,
                                              char **out_mime,
                                              char **out_text,
                                              char **out_blob,
                                              void *ctx)
{
    (void)uri;
    (void)ctx;
    ESP_RETURN_ON_FALSE(out_mime && out_text && out_blob, ESP_ERR_INVALID_ARG, TAG, "Invalid resource output");

    *out_mime = strdup("application/json");
    *out_blob = NULL;
    if (!*out_mime) {
        return ESP_ERR_NO_MEM;
    }

    char payload[1536];
    append_digital_inputs_json(payload, sizeof(payload));
    *out_text = strdup(payload);
    if (!*out_text) {
        free(*out_mime);
        *out_mime = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;

#if CONFIG_FIXTURE_WIFI_MODE_STA
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_sta_retry_count < CONFIG_FIXTURE_WIFI_STA_MAX_RETRY) {
            esp_wifi_connect();
            s_sta_retry_count++;
            ESP_LOGW(TAG, "Retrying Wi-Fi connection (%d/%d)", s_sta_retry_count, CONFIG_FIXTURE_WIFI_STA_MAX_RETRY);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        snprintf(s_ip_addr, sizeof(s_ip_addr), IPSTR, IP2STR(&event->ip_info.ip));
        s_sta_retry_count = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
#else
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_STACONNECTED) {
        wifi_event_ap_staconnected_t *event = (wifi_event_ap_staconnected_t *)event_data;
        ESP_LOGI(TAG, "Station joined AP, aid=%d", event->aid);
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_STADISCONNECTED) {
        wifi_event_ap_stadisconnected_t *event = (wifi_event_ap_stadisconnected_t *)event_data;
        ESP_LOGI(TAG, "Station left AP, aid=%d", event->aid);
    }
#endif
}

#if CONFIG_FIXTURE_WIFI_MODE_AP
static void start_wifi_ap(void)
{
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        NULL));

    uint8_t mac[6] = {0};
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP));

    wifi_config_t wifi_config = {0};
    snprintf((char *)wifi_config.ap.ssid,
             sizeof(wifi_config.ap.ssid),
             "%s-%02X%02X",
             CONFIG_FIXTURE_WIFI_AP_SSID_PREFIX,
             mac[4],
             mac[5]);
    wifi_config.ap.ssid_len = strlen((char *)wifi_config.ap.ssid);
    wifi_config.ap.channel = CONFIG_FIXTURE_WIFI_AP_CHANNEL;
    wifi_config.ap.max_connection = CONFIG_FIXTURE_WIFI_AP_MAX_CONN;
    snprintf((char *)wifi_config.ap.password,
             sizeof(wifi_config.ap.password),
             "%s",
             CONFIG_FIXTURE_WIFI_AP_PASSWORD);
    const size_t ap_password_len = strlen(CONFIG_FIXTURE_WIFI_AP_PASSWORD);
    ESP_ERROR_CHECK((ap_password_len == 0 || ap_password_len >= 8) ? ESP_OK : ESP_ERR_INVALID_ARG);
    wifi_config.ap.authmode = ap_password_len == 0 ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    snprintf(s_ip_addr, sizeof(s_ip_addr), "192.168.4.1");
    ESP_LOGI(TAG, "SoftAP started: ssid=%s password=%s ip=%s",
             wifi_config.ap.ssid,
             strlen(CONFIG_FIXTURE_WIFI_AP_PASSWORD) ? "<configured>" : "<open>",
             s_ip_addr);
}
#endif

#if CONFIG_FIXTURE_WIFI_MODE_STA
static void start_wifi_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(s_wifi_event_group ? ESP_OK : ESP_ERR_NO_MEM);

    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        NULL));

    wifi_config_t wifi_config = {0};
    snprintf((char *)wifi_config.sta.ssid, sizeof(wifi_config.sta.ssid), "%s", CONFIG_FIXTURE_WIFI_STA_SSID);
    snprintf((char *)wifi_config.sta.password, sizeof(wifi_config.sta.password), "%s", CONFIG_FIXTURE_WIFI_STA_PASSWORD);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
                                           WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                           pdFALSE,
                                           pdFALSE,
                                           pdMS_TO_TICKS(30000));

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "Connected to Wi-Fi ssid=%s ip=%s", CONFIG_FIXTURE_WIFI_STA_SSID, s_ip_addr);
    } else {
        ESP_LOGE(TAG, "Failed to connect to Wi-Fi ssid=%s", CONFIG_FIXTURE_WIFI_STA_SSID);
        abort();
    }
}
#endif

static void init_wifi(void)
{
#if CONFIG_FIXTURE_WIFI_MODE_STA
    start_wifi_sta();
#else
    start_wifi_ap();
#endif
}

static void init_fixture_io(void)
{
    warn_if_boot_strapping_gpio("DUT reset", CONFIG_FIXTURE_RESET_GPIO);
    warn_if_boot_strapping_gpio("Load switch", CONFIG_FIXTURE_LOAD_SWITCH_GPIO);
    warn_if_boot_strapping_gpio("MUX SEL0", CONFIG_FIXTURE_MUX_SEL0_GPIO);
    warn_if_boot_strapping_gpio("MUX SEL1", CONFIG_FIXTURE_MUX_SEL1_GPIO);
    warn_if_boot_strapping_gpio("MUX SEL2", CONFIG_FIXTURE_MUX_SEL2_GPIO);

    configure_output_gpio(CONFIG_FIXTURE_RESET_GPIO, CONFIG_FIXTURE_RESET_ACTIVE_LOW ? 1 : 0);
    configure_output_gpio(CONFIG_FIXTURE_LOAD_SWITCH_GPIO, CONFIG_FIXTURE_LOAD_SWITCH_ACTIVE_HIGH ? 0 : 1);
    configure_output_gpio(CONFIG_FIXTURE_MUX_SEL0_GPIO, 0);
    configure_output_gpio(CONFIG_FIXTURE_MUX_SEL1_GPIO, 0);
    configure_output_gpio(CONFIG_FIXTURE_MUX_SEL2_GPIO, 0);
    set_mux_channel(0);
    set_load_switch(false);

    for (size_t i = 0; i < sizeof(s_digital_inputs) / sizeof(s_digital_inputs[0]); i++) {
        if (digital_input_entry_is_enabled(&s_digital_inputs[i])) {
            warn_if_boot_strapping_gpio(s_digital_inputs[i].label, s_digital_inputs[i].gpio);
            configure_input_gpio(i);
        }
    }
}

#if CONFIG_FIXTURE_ADC_ENABLE
static bool adc_channel_is_supported(void)
{
    if (CONFIG_FIXTURE_ADC_UNIT == 1) {
        return CONFIG_FIXTURE_ADC_CHANNEL >= 0 && CONFIG_FIXTURE_ADC_CHANNEL <= 7;
    }
    if (CONFIG_FIXTURE_ADC_UNIT == 2) {
        return CONFIG_FIXTURE_ADC_CHANNEL >= 0 && CONFIG_FIXTURE_ADC_CHANNEL <= 9;
    }
    return false;
}

static adc_unit_t adc_unit_id(void)
{
    return CONFIG_FIXTURE_ADC_UNIT == 1 ? ADC_UNIT_1 : ADC_UNIT_2;
}

static void init_adc_calibration(void)
{
    s_adc_cali_ready = false;
    snprintf(s_adc_cali_error, sizeof(s_adc_cali_error), "disabled");

    if (!CONFIG_FIXTURE_ADC_CALIBRATION_ENABLE) {
        return;
    }

#if ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    adc_cali_line_fitting_config_t cali_config = {
        .unit_id = adc_unit_id(),
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
#if CONFIG_IDF_TARGET_ESP32
        .default_vref = CONFIG_FIXTURE_ADC_DEFAULT_VREF_MV,
#endif
    };
    esp_err_t ret = adc_cali_create_scheme_line_fitting(&cali_config, &s_adc_cali_handle);
    if (ret != ESP_OK) {
        snprintf(s_adc_cali_error, sizeof(s_adc_cali_error), "%s", esp_err_to_name(ret));
        ESP_LOGW(TAG, "ADC calibration unavailable: %s", s_adc_cali_error);
        return;
    }

    s_adc_cali_ready = true;
    s_adc_cali_error[0] = '\0';
#else
    snprintf(s_adc_cali_error, sizeof(s_adc_cali_error), "unsupported_scheme");
    ESP_LOGW(TAG, "ADC line-fitting calibration is not supported on this target");
#endif
}

static void init_adc(void)
{
    s_adc_ready = false;
    snprintf(s_adc_error, sizeof(s_adc_error), "not_initialized");
    s_adc_cali_ready = false;
    snprintf(s_adc_cali_error, sizeof(s_adc_cali_error), "not_initialized");

    if (!adc_channel_is_supported()) {
        snprintf(s_adc_error, sizeof(s_adc_error), "unsupported_channel");
        ESP_LOGE(TAG,
                 "ADC unit/channel unsupported: unit=%d channel=%d",
                 CONFIG_FIXTURE_ADC_UNIT,
                 CONFIG_FIXTURE_ADC_CHANNEL);
        return;
    }

    adc_oneshot_unit_init_cfg_t init_config = {
        .unit_id = adc_unit_id(),
    };
    esp_err_t ret = adc_oneshot_new_unit(&init_config, &s_adc_handle);
    if (ret != ESP_OK) {
        snprintf(s_adc_error, sizeof(s_adc_error), "%s", esp_err_to_name(ret));
        ESP_LOGE(TAG, "ADC unit init failed: %s", s_adc_error);
        return;
    }

    adc_oneshot_chan_cfg_t config = {
        .bitwidth = ADC_BITWIDTH_DEFAULT,
        .atten = ADC_ATTEN_DB_12,
    };
    ret = adc_oneshot_config_channel(s_adc_handle, (adc_channel_t)CONFIG_FIXTURE_ADC_CHANNEL, &config);
    if (ret != ESP_OK) {
        snprintf(s_adc_error, sizeof(s_adc_error), "%s", esp_err_to_name(ret));
        ESP_LOGE(TAG, "ADC channel config failed: %s", s_adc_error);
        return;
    }

    s_adc_ready = true;
    s_adc_error[0] = '\0';
    init_adc_calibration();
}
#endif

static void add_tool_or_abort(esp_mcp_t *mcp, esp_mcp_tool_t *tool)
{
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));
}

static void register_fixture_tools(esp_mcp_t *mcp)
{
    esp_mcp_tool_t *tool = esp_mcp_tool_create("fixture.ping",
                                               "Check that the ESP32 fixture MCP server is alive.",
                                               ping_callback);
    add_tool_or_abort(mcp, tool);

    tool = esp_mcp_tool_create("fixture.get_status",
                               "Read fixture status including IP, uptime, MUX channel and GPIO configuration.",
                               get_status_callback);
    add_tool_or_abort(mcp, tool);

    tool = esp_mcp_tool_create("fixture.self_test",
                               "Run non-destructive fixture configuration and runtime checks.",
                               self_test_callback);
    add_tool_or_abort(mcp, tool);

    tool = esp_mcp_tool_create("fixture.set_mux_channel",
                               "Select an allowlisted fixture MUX channel.",
                               set_mux_channel_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("channel", 0, 0, CONFIG_FIXTURE_MUX_MAX_CHANNEL)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.5,\"risk\":\"medium\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

    tool = esp_mcp_tool_create("fixture.select_net",
                               "Select a fixture MUX channel by configured board net or testpoint label.",
                               select_net_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_string("net", CONFIG_FIXTURE_NET0_LABEL)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.7,\"risk\":\"medium\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

    tool = esp_mcp_tool_create("fixture.set_runtime_net",
                               "Persistently map a board net/testpoint label to a fixture MUX channel.",
                               set_runtime_net_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_string("net", CONFIG_FIXTURE_NET0_LABEL)));
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("channel", 0, 0, CONFIG_FIXTURE_MUX_MAX_CHANNEL)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.75,\"risk\":\"medium\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

    tool = esp_mcp_tool_create("fixture.clear_runtime_net",
                               "Remove one persisted runtime net/testpoint mapping by label.",
                               clear_runtime_net_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_string("net", CONFIG_FIXTURE_NET0_LABEL)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.7,\"risk\":\"medium\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

    tool = esp_mcp_tool_create("fixture.clear_runtime_net_map",
                               "Remove all persisted runtime net/testpoint mappings.",
                               clear_runtime_net_map_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.65,\"risk\":\"medium\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

    tool = esp_mcp_tool_create("fixture.reset_dut",
                               "Pulse the DUT reset line within the configured maximum duration.",
                               reset_dut_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("pulse_ms", 100, 10, CONFIG_FIXTURE_MAX_RESET_PULSE_MS)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.4,\"risk\":\"medium\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

    tool = esp_mcp_tool_create("fixture.set_load_switch",
                               "Enable or disable the fixture-controlled load switch.",
                               set_load_switch_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_bool("enabled", false)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.4,\"risk\":\"high\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

    tool = esp_mcp_tool_create("fixture.read_digital_input",
                               "Read one configured digital input by label.",
                               read_digital_input_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_string("label", CONFIG_FIXTURE_DIN0_LABEL)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.75,\"risk\":\"low\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

    tool = esp_mcp_tool_create("fixture.scan_digital_inputs",
                               "Read all configured digital inputs.",
                               scan_digital_inputs_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.8,\"risk\":\"low\"}"));
    ESP_ERROR_CHECK(esp_mcp_add_tool(mcp, tool));

#if CONFIG_FIXTURE_ADC_ENABLE
    tool = esp_mcp_tool_create("fixture.read_adc_raw",
                               "Read averaged raw ADC samples from the configured fixture ADC channel.",
                               read_adc_raw_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("samples", 8, 1, 64)));
    add_tool_or_abort(mcp, tool);

    tool = esp_mcp_tool_create("fixture.read_net_adc_raw",
                               "Select a configured net/testpoint, wait for MUX settling and read averaged raw ADC samples.",
                               read_net_adc_raw_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_string("net", CONFIG_FIXTURE_NET0_LABEL)));
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("samples", 8, 1, 64)));
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("settle_ms",
                                                   CONFIG_FIXTURE_MUX_SETTLE_MS,
                                                   0,
                                                   CONFIG_FIXTURE_MAX_MUX_SETTLE_MS)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.8,\"risk\":\"medium\"}"));
    add_tool_or_abort(mcp, tool);

    tool = esp_mcp_tool_create("fixture.scan_net_adc",
                               "Scan all configured net/testpoint labels and return a bounded ADC snapshot.",
                               scan_net_adc_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("samples", 8, 1, 64)));
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("settle_ms",
                                                   CONFIG_FIXTURE_MUX_SETTLE_MS,
                                                   0,
                                                   CONFIG_FIXTURE_MAX_MUX_SETTLE_MS)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.85,\"risk\":\"medium\"}"));
    add_tool_or_abort(mcp, tool);

    tool = esp_mcp_tool_create("fixture.sample_net_adc_series",
                               "Select a configured net/testpoint and return a bounded time series of averaged ADC samples.",
                               sample_net_adc_series_callback);
    ESP_ERROR_CHECK(tool ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_string("net", CONFIG_FIXTURE_NET0_LABEL)));
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("points", 4, 1, CONFIG_FIXTURE_ADC_SERIES_MAX_POINTS)));
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("samples_per_point",
                                                   4,
                                                   1,
                                                   CONFIG_FIXTURE_ADC_SERIES_MAX_SAMPLES_PER_POINT)));
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("interval_ms",
                                                   20,
                                                   0,
                                                   CONFIG_FIXTURE_ADC_SERIES_MAX_INTERVAL_MS)));
    ESP_ERROR_CHECK(esp_mcp_tool_add_property(
        tool,
        esp_mcp_property_create_with_int_and_range("settle_ms",
                                                   CONFIG_FIXTURE_MUX_SETTLE_MS,
                                                   0,
                                                   CONFIG_FIXTURE_MAX_MUX_SETTLE_MS)));
    ESP_ERROR_CHECK(esp_mcp_tool_set_annotations_json(
        tool,
        "{\"audience\":[\"assistant\"],\"priority\":0.85,\"risk\":\"medium\"}"));
    add_tool_or_abort(mcp, tool);
#endif
}

static void register_fixture_resources(esp_mcp_t *mcp)
{
    esp_mcp_resource_t *resource = esp_mcp_resource_create("fixture://status",
                                                           "fixture.status",
                                                           "Fixture Status",
                                                           "Current ESP32 fixture status and configured GPIOs.",
                                                           "application/json",
                                                           read_status_resource,
                                                           NULL);
    ESP_ERROR_CHECK(resource ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_resource_set_annotations(resource, "[\"assistant\",\"user\"]", 0.8, NULL));
    ESP_ERROR_CHECK(esp_mcp_add_resource(mcp, resource));

    resource = esp_mcp_resource_create("fixture://net-map",
                                       "fixture.net_map",
                                       "Fixture Net Map",
                                       "Configured board net or testpoint labels mapped to MUX channels.",
                                       "application/json",
                                       read_net_map_resource,
                                       NULL);
    ESP_ERROR_CHECK(resource ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_resource_set_annotations(resource, "[\"assistant\",\"user\"]", 0.9, NULL));
    ESP_ERROR_CHECK(esp_mcp_add_resource(mcp, resource));

    resource = esp_mcp_resource_create("fixture://digital-inputs",
                                       "fixture.digital_inputs",
                                       "Fixture Digital Inputs",
                                       "Configured digital input labels mapped to ESP32 GPIOs.",
                                       "application/json",
                                       read_digital_inputs_resource,
                                       NULL);
    ESP_ERROR_CHECK(resource ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mcp_resource_set_annotations(resource, "[\"assistant\",\"user\"]", 0.8, NULL));
    ESP_ERROR_CHECK(esp_mcp_add_resource(mcp, resource));
}

static void start_mcp_server(void)
{
    esp_mcp_t *mcp = NULL;
    ESP_ERROR_CHECK(esp_mcp_create(&mcp));
    ESP_ERROR_CHECK(esp_mcp_set_server_info(mcp,
                                            "AI Hardware ESP32 Fixture",
                                            "Allowlisted fixture control MCP server for board diagnostics.",
                                            NULL,
                                            "https://github.com/HongfeiWan/ai-hardware"));
    ESP_ERROR_CHECK(esp_mcp_set_instructions(
        mcp,
        "Use fixture tools only for low-level fixture actions. Instrument control and model diagnosis belong on the Python bench server."));

    register_fixture_tools(mcp);
    register_fixture_resources(mcp);

    httpd_config_t http_config = HTTPD_DEFAULT_CONFIG();
    http_config.server_port = CONFIG_FIXTURE_MCP_PORT;
    http_config.stack_size = 8192;
    http_config.max_uri_handlers = 16;

    esp_mcp_mgr_config_t mcp_mgr_config = {
        .transport = esp_mcp_transport_http_server,
        .config = &http_config,
        .instance = mcp,
    };

    esp_mcp_mgr_handle_t mcp_mgr_handle = 0;
    ESP_ERROR_CHECK(esp_mcp_mgr_init(mcp_mgr_config, &mcp_mgr_handle));
    ESP_ERROR_CHECK(esp_mcp_mgr_start(mcp_mgr_handle));
    ESP_ERROR_CHECK(esp_mcp_mgr_register_endpoint(mcp_mgr_handle, CONFIG_FIXTURE_MCP_ENDPOINT, NULL));

    ESP_LOGI(TAG,
             "MCP server ready: http://%s:%d/%s",
             s_ip_addr,
             CONFIG_FIXTURE_MCP_PORT,
             CONFIG_FIXTURE_MCP_ENDPOINT);
}

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_INFO);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    load_runtime_net_map_from_nvs();
    init_fixture_io();
#if CONFIG_FIXTURE_ADC_ENABLE
    init_adc();
#endif
    init_wifi();
    start_mcp_server();
}
