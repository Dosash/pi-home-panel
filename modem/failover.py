#!/usr/bin/env python3
"""Резервный интернет: Wi-Fi основной, LTE-модем — запасной.

NetworkManager уже держит оба маршрута по умолчанию: Wi-Fi с метрикой 600, модем с 700,
так что при пропаже самого Wi-Fi трафик уходит в модем без нас. Этот сторож закрывает
второй случай — Wi-Fi подключён, но интернета через него нет (упал провайдер, завис роутер).
Тогда модему временно ставится метрика ниже Wi-Fi, а когда Wi-Fi оживает — возвращается как было.

Если Wi-Fi раздаёт свою точку доступа (профиль hotspot, см. ~/project/network), он не канал
в интернет: модем — единственный, проверять Wi-Fi незачем.

Состояние пишется в /run/internet-failover/state.json — его показывает панель.
"""
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

WIFI = "wlan0"
MODEM_CONNECTION = "lte-modem"
PRIMARY_METRIC = 50  # модем становится основным
BACKUP_METRIC = 700  # модем в резерве (у Wi-Fi 600)
CHECK_EVERY = 10  # сек
FAILS_TO_SWITCH = 3  # подряд неудачных проверок Wi-Fi, чтобы уйти на модем
OKS_TO_RETURN = 3  # подряд удачных, чтобы вернуться на Wi-Fi
TARGETS = ["8.8.8.8", "77.88.8.8", "1.1.1.1"]  # ping; некоторые операторы режут ICMP до части адресов
HTTP_HOST = "connectivitycheck.gstatic.com"
HTTP_CHECK = f"http://{HTTP_HOST}/generate_204"  # если ping не прошёл ни до одного
STATE = Path("/run/internet-failover/state.json")

log = logging.getLogger("failover")


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def modem_device():
    """Интерфейс, на котором сейчас поднят профиль модема (eth1 и т.п.), или None."""
    out = run("nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device").stdout
    for line in out.splitlines():
        dev, state, conn = (line.split(":") + ["", "", ""])[:3]
        if conn == MODEM_CONNECTION and state == "connected":
            return dev
    return None


def wifi_mode():
    """Чем занят Wi-Fi: client — подключён к роутеру, hotspot — раздаёт свою сеть, None — ничем."""
    conn = run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", WIFI).stdout.strip()
    if not conn:
        return None
    mode = run("nmcli", "-g", "802-11-wireless.mode", "connection", "show", conn).stdout.strip()
    return "hotspot" if mode == "ap" else "client"


def modem_metric(device):
    """Метрика маршрута по умолчанию через модем, или None, если такого маршрута нет."""
    routes = json.loads(run("ip", "-j", "route", "show", "default", "dev", device).stdout or "[]")
    return routes[0].get("metric", 0) if routes else None


def resolve_via(device, host):
    """IPv4-адрес host у DNS-серверов этого интерфейса, запрос тоже идёт через него; None — не вышло.

    curl --interface привязывает к интерфейсу только соединение, а имя спрашивает у системы —
    по основному маршруту: при мёртвом Wi-Fi ответ приходит, когда curl уже сдался."""
    servers = run("nmcli", "-g", "IP4.DNS", "device", "show", device).stdout.replace("|", " ").split()
    if not servers:
        return None
    qid = os.urandom(2)
    query = (qid + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"  # рекурсивный запрос, один вопрос
             + b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\x00\x00\x01\x00\x01")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode())
            s.settimeout(2)
            for server in servers:  # всем сразу, берём первый нормальный ответ
                s.sendto(query, (server, 53))
            while True:
                resp, addr = s.recvfrom(512)
                if resp[:2] == qid and addr[0] in servers and resp[3] & 0x0F == 0:
                    break
        pos = len(query)  # ответ начинается с того же вопроса, дальше записи
        for _ in range(int.from_bytes(resp[6:8], "big")):
            while resp[pos] and resp[pos] < 0xC0:  # имя: метки до нуля или ссылка из 2 байт
                pos += resp[pos] + 1
            pos += 2 if resp[pos] else 1
            rtype, rdlen = resp[pos:pos + 2], int.from_bytes(resp[pos + 8:pos + 10], "big")
            pos += 10
            if rtype == b"\x00\x01" and rdlen == 4:  # A; CNAME и прочее пропускаем
                return socket.inet_ntoa(resp[pos:pos + 4])
            pos += rdlen
    except (OSError, IndexError):  # таймаут, нет маршрута, битый ответ
        pass
    return None


def online_via(device):
    """Есть ли интернет именно через этот интерфейс: ping с привязкой к нему."""
    if not device:
        return False
    for target in TARGETS:
        if run("ping", "-c", "1", "-W", "2", "-I", device, target).returncode == 0:
            return True
    # Имя разрешаем через тот же интерфейс; не вышло — как раньше, системным резолвером.
    ip = resolve_via(device, HTTP_HOST)
    resolve = ["--resolve", f"{HTTP_HOST}:80:{ip}"] if ip else []
    res = run("curl", "-s", "-o", "/dev/null", "-m", "8", "--interface", device, *resolve,
              "-w", "%{http_code}", HTTP_CHECK)
    return res.stdout.strip() == "204"


def set_modem_metric(device, metric):
    # Меняем только текущую активацию (без записи в профиль): после перезагрузки снова резерв.
    res = run("nmcli", "device", "modify", device, "ipv4.route-metric", str(metric))
    if res.returncode != 0:
        log.error("nmcli device modify %s: %s", device, res.stderr.strip())
        return False
    return True


def write_state(state):
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    os.chmod(tmp, 0o644)
    tmp.replace(STATE)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # --simulate-wifi-down: проверка переключения — считаем, что через Wi-Fi интернета нет.
    simulate = "--simulate-wifi-down" in sys.argv
    # После сбоя или перезапуска начинаем с чистого листа: модем — резерв (метрику поправит цикл).
    active = "wifi"
    fails = oks = 0
    since = time.time()
    while True:
        modem = modem_device()
        # Метрику сверяем каждый круг: NM при переподключении модема (дребезг, USB) берёт её
        # из профиля, а после прошлого запуска могла остаться 50.
        want = PRIMARY_METRIC if active == "modem" else BACKUP_METRIC
        if modem and (metric := modem_metric(modem)) is not None and metric != want:
            log.warning("У модема (%s) метрика %s вместо %s — исправляю", modem, metric, want)
            set_modem_metric(modem, want)
        mode = wifi_mode()
        modem_ok = online_via(modem)
        if mode == "hotspot":
            # Wi-Fi раздаёт точку доступа: интернет только через модем, сравнивать не с чем.
            wifi_ok, fails, oks = None, 0, 0
            if active != "modem":
                log.warning("Wi-Fi раздаёт точку доступа — интернет только через модем (%s)", modem)
                active, since = "modem", time.time()
        else:
            wifi_ok = False if simulate else online_via(WIFI)
            if wifi_ok:
                fails, oks = 0, oks + 1
            else:
                fails, oks = fails + 1, 0

            if active == "wifi" and fails >= FAILS_TO_SWITCH and modem_ok:
                if set_modem_metric(modem, PRIMARY_METRIC):
                    log.warning("Через Wi-Fi нет интернета — перехожу на модем (%s)", modem)
                    active, since = "modem", time.time()
            elif active == "modem" and (oks >= OKS_TO_RETURN or not modem):
                if not modem or set_modem_metric(modem, BACKUP_METRIC):
                    log.warning("Wi-Fi снова работает — возвращаюсь на него")
                    active, since = "wifi", time.time()

        write_state({
            "time": time.time(),
            "active": active,  # через что сейчас идёт интернет: wifi | modem
            "since": since,
            "wifi_ok": wifi_ok,  # None — Wi-Fi раздаёт точку доступа и не проверяется
            "wifi_mode": mode,  # client | hotspot | None
            "modem_ok": modem_ok,
            "modem_device": modem,
            "check_every": CHECK_EVERY,
        })
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
