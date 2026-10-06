#!/usr/bin/env python3
"""Известные Zigbee-адаптеры (USB VID:PID) — берутся из установленного zigbee-herdsman.

Нужно, чтобы Zigbee2MQTT запускался только при вставленном Zigbee-адаптере, а не от любого
USB-устройства с последовательным портом (например, 4G-модема).

  adapters.py --check   код выхода 0, если Zigbee2MQTT найдёт вставленный адаптер (ExecCondition)
  adapters.py --rules   печатает правило udev для /etc/udev/rules.d/99-zigbee-adapter.rules
  adapters.py           список известных и вставленных адаптеров
"""
import glob
import re
import subprocess
import sys
from pathlib import Path

Z2M = "/opt/zigbee2mqtt"
DISCOVERY = (Z2M + "/node_modules/.pnpm/zigbee-herdsman@*/node_modules/"
             "zigbee-herdsman/dist/adapter/adapterDiscovery.js")
# Та же функция, которой ищет адаптер сам Zigbee2MQTT: VID:PID + производитель/название + порт.
# VID:PID мало — CP2102 или CH340 стоят и в куче других устройств.
HERDSMAN_CHECK = ('require("zigbee-herdsman/dist/adapter/adapterDiscovery").findUsbAdapter()'
                  '.then(m => process.exit(m ? 0 : 1), () => process.exit(1))')


def known():
    pairs = set()
    for path in glob.glob(DISCOVERY):
        src = "\n".join(line for line in Path(path).read_text().splitlines()
                        if not line.strip().startswith("//"))
        pairs |= {(v.lower(), p.lower()) for v, p in re.findall(
            r'vendorId:\s*"([0-9a-fA-F]{4})",\s*productId:\s*"([0-9a-fA-F]{4})"', src)}
    return sorted(pairs)


def plugged():
    found = set()
    for dev in Path("/sys/bus/usb/devices").iterdir():
        try:
            found.add(((dev / "idVendor").read_text().strip(), (dev / "idProduct").read_text().strip()))
        except OSError:
            pass
    return found


def herdsman_finds_adapter():
    try:
        return subprocess.run(["node", "-e", HERDSMAN_CHECK], cwd=Z2M, capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main():
    pairs = known()
    if "--check" in sys.argv:
        # Сначала дешёвый фильтр по VID:PID, node запускаем, только если есть кандидат.
        sys.exit(0 if set(pairs) & plugged() and herdsman_finds_adapter() else 1)
    if "--rules" in sys.argv:
        print("# Создано adapters.py --rules: вставили известный Zigbee-адаптер — запускаем Zigbee2MQTT.")
        for vid, pid in pairs:
            print(f'ACTION=="add", SUBSYSTEM=="tty", ENV{{ID_VENDOR_ID}}=="{vid}", '
                  f'ENV{{ID_MODEL_ID}}=="{pid}", TAG+="systemd", ENV{{SYSTEMD_WANTS}}+="zigbee2mqtt.service"')
        return
    now = plugged()
    for vid, pid in pairs:
        print(f"{vid}:{pid}{'  <- вставлен' if (vid, pid) in now else ''}")


if __name__ == "__main__":
    main()
