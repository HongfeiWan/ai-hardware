idf_component_register(
    SRCS "app_main.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_adc
        esp_driver_gpio
        esp_event
        esp_http_server
        esp_timer
        esp_wifi
        esp_netif
        nvs_flash
        mcp-c-sdk
)
