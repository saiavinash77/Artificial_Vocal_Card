# AVC ESP32-S3 firmware — top-level ESP-IDF project.
#
# Build (ESP-IDF v5.x):
#   cd firmware
#   idf.py set-target esp32s3
#   idf.py menuconfig     # set AVC gateway IP/port + WiFi credentials
#   idf.py build
#   idf.py -p COMx flash monitor
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(avc_firmware)
