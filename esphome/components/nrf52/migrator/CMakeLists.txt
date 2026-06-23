# Standalone Zephyr build for the fw1 -> fw2 (Option B) migrator.
#
# Built for the xiao_ble board to produce the unsigned base image. The ESPHome
# nrf52 build invokes this automatically (west build) during a migrator firmware
# compile, then patches the embedded fw2-bootloader blob into the result and
# signs it (see ../xiao_ble_mcuboot_migrator.py.script and README.md). No
# prebuilt binary is committed.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(xiao_ble_mcuboot_migrator LANGUAGES C)

target_sources(app PRIVATE
  src/main.c
  src/migrate.c
  src/blob.c
)

target_include_directories(app PRIVATE src)
