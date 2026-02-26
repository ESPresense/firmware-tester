FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    curl \
    git \
    libusb-1.0-0 \
    uhubctl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir platformio esptool pyserial "click<8.2.0"

ENV PLATFORMIO_CACHE_DIR=/cache/platformio

COPY scripts/hil_monitor.py /scripts/hil_monitor.py
COPY scripts/improv_wifi.py /scripts/improv_wifi.py
