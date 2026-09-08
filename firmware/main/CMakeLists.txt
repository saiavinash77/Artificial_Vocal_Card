# Main AVC firmware component: packet serializer + onset gate + app.
idf_component_register(
    SRCS "packet.c" "onset.c" "main.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event nvs_flash esp_timer lwip driver
)
