# syntax=docker/dockerfile:1
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    curl \
    git \
    libusb-1.0-0 \
    uhubctl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir platformio esptool pyserial "click<8.2.0"

ENV PLATFORMIO_CACHE_DIR=/cache/platformio

RUN mkdir /tmp/pio-seed && cat > /tmp/pio-seed/platformio.ini <<'EOF'
[env:esp32]
platform = https://github.com/pioarduino/platform-espressif32/releases/download/54.03.21/platform-espressif32.zip
board = esp32dev

[env:esp32s3]
platform = https://github.com/pioarduino/platform-espressif32/releases/download/54.03.21/platform-espressif32.zip
board = esp32s3box

[env:esp32c3]
platform = https://github.com/pioarduino/platform-espressif32/releases/download/54.03.21/platform-espressif32.zip
board = esp32-c3-devkitm-1
EOF

RUN cd /tmp/pio-seed && pio pkg install && rm -rf /tmp/pio-seed

COPY scripts/hil_monitor.py /scripts/hil_monitor.py
COPY scripts/improv_wifi.py /scripts/improv_wifi.py
