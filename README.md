# firmware-tester

Hardware-in-the-loop harness for [ESPresense](https://github.com/ESPresense/ESPresense) firmware, run by CrowCI (Woodpecker) against real ESP32 / C3 / C6 / S3 boards on the bench.

## What is in here

* `scripts/improv_wifi.py` — provisions WiFi over [Improv Serial](https://www.improv-wifi.com/serial/): requests device info (validates the frames and their checksums), sends the credentials, and expects the `provisioning` state.
* `scripts/hil_monitor.py` — watches the serial console for `--duration` seconds and fails on crash patterns, restarts, missing scan results, the MQTT reconnect limit, a misbehaving `/json` under concurrent load, or a declining heap (see the exit codes at the top of the file). `--dma-bisect` runs the A/B browser-load heap test.
* `scripts/test_*.py` — host-side tests for the monitor's heuristics (`python3 -m pytest scripts`).
* `Dockerfile` — the `ghcr.io/espresense/firmware-tester` image (PlatformIO + the scripts) used by the Arduino-era pipeline. The ESP-IDF firmware runs these scripts from the `espressif/idf` image and clones this repo at run time.

## Running by hand

```sh
pip install pyserial
python3 scripts/improv_wifi.py --port /dev/ttyUSB0 --ssid MyWifi --password secret
python3 scripts/hil_monitor.py --port /dev/ttyUSB0 --duration 180
```

## Releasing the image

`gh release create vX.Y.Z --target <sha>` — `docker.yml` builds and pushes `:1`, `:X`, `:X.Y`; the pipeline pins the floating major tag.
