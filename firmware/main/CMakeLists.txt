# Main AVC firmware component: packet serializer + acquisition app.
idf_component_register(
    SRCS "packet.c" "main.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event nvs_flash esp_timer lwip
)
