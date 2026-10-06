"""Wi-Fi Pi для панели: точка доступа, кто к ней подключён, и её настройки.

Точку доступа держит NetworkManager, профиль hotspot (см. ~/project/network/README.md).
Клиентов, их сигнал и трафик видно через iw, имена — в арендах DHCP у dnsmasq.
Менять профиль, читать пароль и аренды может только root, поэтому эти команды идут через
sudo -n (у dosash он без пароля). Без sudo блок покажет всё, кроме имён устройств и пароля.
"""
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("webcam")

DEV = "wlan0"
PROFILE = "hotspot"
LEASES = Path(f"/var/lib/NetworkManager/dnsmasq-{DEV}.leases")
IW = shutil.which("iw") or "/usr/sbin/iw"
SUDO = ["sudo", "-n"]
CACHE_SECONDS = 2  # не дёргаем nmcli и iw чаще, сколько бы страниц ни было открыто
# 5 ГГц — только каналы без DFS: на остальных точка доступа сначала минуту слушает эфир на радары.
CHANNELS = {"bg": list(range(1, 14)), "a": [36, 40, 44, 48]}
VENDOR = Path(__file__).resolve().parent / "vendor"  # segno — QR-коды, чистый Python, BSD (vendor/segno/LICENSE)
SETTINGS = ("802-11-wireless.ssid", "802-11-wireless.band", "802-11-wireless.channel",
            "802-11-wireless-security.psk")


def _run(cmd, timeout=5):
    """(код возврата, stdout, stderr); не запустилось или зависло — код 1 и текст ошибки."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def _unescape(value):
    # nmcli -g экранирует «:» и «\» обратной косой чертой
    return re.sub(r"\\(.)", r"\1", value)


def _int(text):
    m = re.match(r"-?\d+", text or "")
    return int(m.group()) if m else None


def stations():
    """Подключённые клиенты из iw: {mac: {...}}. Поля, которых драйвер не даёт, — None."""
    _, out, _ = _run([IW, "dev", DEV, "station", "dump"])
    result, cur = {}, None
    for line in out.splitlines():
        if line.startswith("Station "):
            mac = line.split()[1].lower()
            cur = result[mac] = {"mac": mac}
            continue
        if cur is None or ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip()
        if key == "signal":
            cur["signal"] = _int(value)
        elif key == "rx bytes":  # принято от устройства — то, что оно отправило
            cur["up"] = _int(value)
        elif key == "tx bytes":  # отправлено устройству — то, что оно скачало
            cur["down"] = _int(value)
        elif key == "tx bitrate":
            m = re.match(r"[\d.]+", value)
            cur["rate"] = float(m.group()) if m else None
        elif key == "connected time":
            cur["connected"] = _int(value)
        elif key == "inactive time":
            cur["inactive_ms"] = _int(value)
    return result


def leases():
    """Аренды DHCP точки доступа: {mac: (ip, имя)}. Нужен root — без sudo пусто."""
    rc, out, _ = _run(SUDO + ["cat", str(LEASES)])
    found = {}
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split()
        if len(parts) >= 4:
            found[parts[1].lower()] = (parts[2], None if parts[3] == "*" else parts[3])
    return found


def neighbours():
    """{mac: ip} по ARP — адрес устройства, даже если аренду прочитать не вышло."""
    _, out, _ = _run(["ip", "-4", "neigh", "show", "dev", DEV])
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if "lladdr" in parts:
            found[parts[parts.index("lladdr") + 1].lower()] = parts[0]
    return found


def address():
    _, out, _ = _run(["ip", "-j", "-4", "addr", "show", "dev", DEV])
    try:
        info = json.loads(out or "[]")
    except ValueError:
        return None
    return next((a["local"] for i in info for a in i.get("addr_info", []) if a.get("local")), None)


def traffic():
    stats = Path(f"/sys/class/net/{DEV}/statistics")
    try:  # rx — от устройств к Pi, tx — от Pi к устройствам
        return {"up": int((stats / "rx_bytes").read_text()), "down": int((stats / "tx_bytes").read_text())}
    except (OSError, ValueError):
        return None


def validate(ssid, password, band, channel):
    """Проверяет новые настройки; ошибка — ValueError с текстом для страницы."""
    ssid = (ssid or "").strip()
    if not ssid or len(ssid.encode()) > 32:
        raise ValueError("имя сети — от 1 до 32 байт (кириллица — по 2 байта на букву)")
    if any(ord(c) < 32 for c in ssid):
        raise ValueError("в имени сети не должно быть управляющих символов")
    if password is not None:  # None — пароль не меняем
        if not 8 <= len(password) <= 63 or not all(32 <= ord(c) < 127 for c in password):
            raise ValueError("пароль — от 8 до 63 латинских букв, цифр или знаков")
    if band not in CHANNELS:
        raise ValueError("диапазон — 2,4 или 5 ГГц")
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        raise ValueError("канал должен быть числом") from None
    if channel not in CHANNELS[band]:
        raise ValueError(f"в этом диапазоне доступны каналы {', '.join(map(str, CHANNELS[band]))}")
    return ssid, password, band, channel


class Hotspot:
    def __init__(self):
        self.lock = threading.Lock()  # опрос состояния
        self.apply_lock = threading.Lock()  # одно применение настроек за раз
        self.cache = (0.0, None)
        self.job = None  # последнее применение настроек: {"state": applying|done|failed, ...}

    # ---------- настройки профиля ----------

    def settings(self):
        """Текущие настройки профиля. Пароль — только через sudo; без него None."""
        rc, out, _ = _run(SUDO + ["nmcli", "-s", "-g", ",".join(SETTINGS), "connection", "show", PROFILE])
        if rc != 0:  # без sudo — всё, кроме пароля
            rc, out, _ = _run(["nmcli", "-g", ",".join(SETTINGS[:3]), "connection", "show", PROFILE])
            if rc != 0:
                return None
        values = [_unescape(v) for v in out.split("\n")] + [None] * 4
        ssid, band, channel, psk = values[:4]
        return {"ssid": ssid, "band": band or "bg", "channel": _int(channel) or 0, "password": psk or None}

    def _modify(self, ssid, password, band, channel):
        cmd = SUDO + ["nmcli", "connection", "modify", PROFILE, "802-11-wireless.ssid", ssid,
                      "802-11-wireless.band", band, "802-11-wireless.channel", str(channel)]
        if password:
            cmd += ["wifi-sec.psk", password]
        rc, _, err = _run(cmd, timeout=15)
        return None if rc == 0 else (err or "nmcli не смог сохранить настройки")

    def _activate(self, ssid):
        rc, _, err = _run(SUDO + ["nmcli", "--wait", "40", "connection", "up", PROFILE], timeout=50)
        if rc != 0:
            return err.removeprefix("Error: ") or "точка доступа не включилась"
        _, info, _ = _run([IW, "dev", DEV, "info"])
        if "type AP" not in info or f"ssid {ssid}" not in info:
            return "точка доступа не включилась"
        return None

    def apply(self, ssid, password, band, channel):
        """Проверяет и запускает применение в фоне: ответ должен уйти раньше, чем Wi-Fi пропадёт.
        Не включилось — возвращаем прежние настройки. Ход дел — в status()["job"]."""
        ssid, password, band, channel = validate(ssid, password, band, channel)
        old = self.settings()
        if old is None or (old["password"] is None and password is not None):
            raise ValueError("нет доступа к настройкам Wi-Fi (sudo без пароля)")
        if (ssid, band, channel) == (old["ssid"], old["band"], old["channel"]) \
                and password in (None, old["password"]):
            raise ValueError("настройки не изменились")
        if not self.apply_lock.acquire(blocking=False):
            raise ValueError("настройки уже применяются, подождите")
        self.job = {"state": "applying", "time": time.time(), "ssid": ssid}
        threading.Thread(target=self._apply, args=((ssid, password, band, channel), old),
                         daemon=False, name="wifi-apply").start()

    def _apply(self, new, old):
        try:
            time.sleep(1)  # даём ответу дойти до браузера, пока Wi-Fi ещё работает
            log.info("Wi-Fi: применяю «%s», %s, канал %s%s", new[0], new[2], new[3],
                     ", новый пароль" if new[1] else "")
            error = self._modify(*new) or self._activate(new[0])
            if error:
                log.warning("Wi-Fi: не применилось (%s) — возвращаю прежние настройки", error)
                self._modify(old["ssid"], old["password"], old["band"], old["channel"])
                self._activate(old["ssid"])
                self.job = {"state": "failed", "time": time.time(), "error": error, "ssid": old["ssid"]}
            else:
                log.info("Wi-Fi: настройки применены")
                self.job = {"state": "done", "time": time.time(), "ssid": new[0]}
        finally:
            self.cache = (0.0, None)
            self.apply_lock.release()

    def qr_svg(self):
        """SVG с QR-кодом для подключения (WIFI:T:WPA;S:…;P:…;; — понимают камеры iPhone и Android).
        None — пароль не прочитать."""
        st = self.settings()
        if not st or not st["password"]:
            return None
        if str(VENDOR) not in sys.path:
            sys.path.insert(0, str(VENDOR))
        import segno.helpers
        qr = segno.helpers.make_wifi(ssid=st["ssid"], password=st["password"], security="WPA")
        buf = io.BytesIO()
        # Без width/height — размер задаёт страница; чёрное на белом, как ждут сканеры.
        qr.save(buf, kind="svg", border=2, dark="#000", light="#fff", xmldecl=False, omitsize=True)
        return buf.getvalue()

    # ---------- состояние ----------

    def status(self):
        with self.lock:
            at, cached = self.cache
            if cached is None or time.monotonic() - at >= CACHE_SECONDS:
                cached = self._read()
                self.cache = (time.monotonic(), cached)
        return {**cached, "job": self.job}

    def _read(self):
        _, conn, _ = _run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", DEV])
        _, info, _ = _run([IW, "dev", DEV, "info"])
        ap = conn == PROFILE and "type AP" in info
        settings = self.settings()
        # Версия QR-кода: меняется вместе с именем сети или паролем — страница по ней перезапрашивает картинку.
        qr = (hashlib.sha256(f"{settings['ssid']}\0{settings['password']}".encode()).hexdigest()[:12]
              if settings and settings["password"] else None)
        result = {"active": ap, "connection": conn or None, "settings": settings, "qr": qr,
                  "channel": None, "width": None, "txpower": None, "clients": [], "traffic": None}
        if not ap:
            return result
        m = re.search(r"channel (\d+) \((\d+) MHz\), width: (\d+) MHz", info)
        if m:
            result["channel"], result["freq"], result["width"] = map(int, m.groups())
        m = re.search(r"txpower ([\d.]+) dBm", info)
        result["txpower"] = float(m.group(1)) if m else None
        names, arp = leases(), neighbours()
        clients = []
        for mac, st in stations().items():
            ip, name = names.get(mac, (arp.get(mac), None))
            clients.append({**st, "ip": ip or arp.get(mac), "name": name})
        clients.sort(key=lambda c: (c.get("connected") is None, -(c.get("connected") or 0)))
        result["clients"] = clients
        result["traffic"] = traffic()
        result["address"] = address()
        return result
