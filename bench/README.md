# Bench host setup

Host-side pieces for the HIL bench (`tsi-ha`). Everything else in this repo runs inside
the `firmware-tester` container; these do not, because they drive hardware the container
has no access to.

## BLE flood

`scripts/ble_flood.py` advertises from a fresh random address ~40 times a second, so each
rotation costs a node a new fingerprint slot. It exists to make the slow heap decline in
[ESPresense#2309](https://github.com/ESPresense/ESPresense/issues/2309) show up in a HIL
window instead of over days on someone's shelf.

It talks to the adapter over `HCI_CHANNEL_USER`, so **BlueZ is not required and should not
be installed** — the kernel hands over the controller exclusively and `bluetoothd` would
only fight for it.

Requires a USB Bluetooth adapter passed through to the VM. Verify the kernel sees it:

```bash
ls /sys/class/bluetooth        # expect hci0
python3 ble_flood.py --selftest   # framing + address rules, no hardware needed
sudo python3 ble_flood.py --index 0 --seconds 30   # 30s live burst
```

Install:

```bash
sudo install -m 755 scripts/ble_flood.py /usr/local/bin/ble_flood.py
sudo install -m 644 bench/ble-flood.service /etc/systemd/system/
sudo systemctl enable --now ble-flood
```

The service idles until a request file appears in `/var/lock/woodpecker/bleflood.d/`. Each
HIL step creates one on entry and removes it on exit, so the flood only runs during tests —
otherwise every ESPresense node within range spends the day logging junk MACs. It is a
directory, not a single flag, because the four device steps run in parallel and the first
one to finish must not cut the flood out from under the rest. Request files older than
`--max-age` (9h) are ignored, so a hard-killed container cannot leave the bench advertising
forever.

To flood by hand (e.g. reproducing a report):

```bash
sudo mkdir -p /var/lock/woodpecker/bleflood.d
sudo touch /var/lock/woodpecker/bleflood.d/manual   # journalctl -fu ble-flood to watch
sudo rm /var/lock/woodpecker/bleflood.d/manual
```
