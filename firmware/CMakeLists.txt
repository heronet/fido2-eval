cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(fido2_node_bench)

target_sources(app PRIVATE
  src/main.c
  src/stats.c
)
target_include_directories(app PRIVATE src)

if(CONFIG_USB_DEVICE_STACK_NEXT)
  target_sources(app PRIVATE src/usb.c)
endif()

if(CONFIG_FIDO2)
  target_sources(app PRIVATE src/up_timing.c)
endif()
