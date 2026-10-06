"""Интернет и LTE-модем для панели.

Модем — Huawei в режиме HiLink: у него свой веб-API на 192.168.8.1 (XML, токен на каждый запрос).
Какой канал сейчас основной, решает сервис internet-failover (~/project/modem); здесь только
читаем его состояние из /run/internet-failover/state.json.
"""
import json
import logging
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger("webcam")

BASE = "http://192.168.8.1"
TIMEOUT = 3
CACHE_SECONDS = 3  # не дёргаем модем чаще, сколько бы страниц ни было открыто
FAILOVER_STATE = Path("/run/internet-failover/state.json")
WIFI, CABLE = "wlan0", "eth0"  # точка доступа и кабель — см. ~/project/network
IW = shutil.which("iw") or "/usr/sbin/iw"

CONNECTION = {"900": "connecting", "901": "connected", "902": "disconnected", "903": "disconnecting"}
SIM = {"257": "ready", "255": "absent", "260": "pin", "261": "puk", "258": "invalid"}
MODES = {"auto": "00", "2g": "01", "3g": "02", "4g": "03"}
SESSION_ERRORS = {"125001", "125002", "125003"}  # сессия или токен больше не действуют

# CurrentNetworkTypeEx → (поколение, название)
NETWORK_EX = {
    "0": (None, "нет сети"), "1": ("2G", "GSM"), "2": ("2G", "GPRS"), "3": ("2G", "EDGE"),
    "41": ("3G", "WCDMA"), "42": ("3G", "HSDPA"), "43": ("3G", "HSUPA"), "44": ("3G", "HSPA"),
    "45": ("3G", "HSPA+"), "46": ("3G", "DC-HSPA+"), "101": ("4G", "LTE"), "1011": ("4G", "LTE+"),
}
NETWORK_OLD = {  # CurrentNetworkType у старых прошивок
    "0": (None, "нет сети"), "1": ("2G", "GSM"), "2": ("2G", "GPRS"), "3": ("2G", "EDGE"),
    "4": ("3G", "WCDMA"), "5": ("3G", "HSDPA"), "6": ("3G", "HSUPA"), "7": ("3G", "HSPA"),
    "9": ("3G", "HSPA+"), "19": ("4G", "LTE"),
}


class ModemError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class HiLink:
    def __init__(self, base=BASE):
        self.base = base
        self.lock = threading.Lock()  # HiLink не любит параллельные сессии
        self.device = None  # модель и прошивка не меняются — читаем один раз
        self.cache = (0.0, None)
        self.sess = None  # одна сессия на все GET — см. get()

    def _session(self):
        root = ET.fromstring(urllib.request.urlopen(
            self.base + "/api/webserver/SesTokInfo", timeout=TIMEOUT).read())
        return {"Cookie": root.findtext("SesInfo") or "",
                "__RequestVerificationToken": root.findtext("TokInfo") or ""}

    def _parse(self, raw):
        root = ET.fromstring(raw)
        if root.tag == "error":
            code = root.findtext("code")
            raise ModemError(f"модем ответил ошибкой {code}", code)
        if root.text and root.text.strip() == "OK":
            return {}
        return {el.tag: (el.text or "").strip() for el in root}

    def get(self, path):
        # Модем помнит только 17 последних сессий: новая на каждый запрос выбивала бы
        # его веб-интерфейс и прочих клиентов. Для GET хватает одной, пока модем её принимает.
        for fresh in (False, True):
            if fresh or self.sess is None:
                self.sess = self._session()
            req = urllib.request.Request(self.base + path, headers=self.sess)
            try:
                return self._parse(urllib.request.urlopen(req, timeout=TIMEOUT).read())
            except ModemError as e:
                # сессию вытеснили или модем перезагрузился — один раз повторяем с новой
                if fresh or e.code not in SESSION_ERRORS:
                    raise
            except OSError:
                self.sess = None  # модем мог перезагрузиться — старая сессия уже не годится
                raise

    def post(self, path, fields):
        body = ("<?xml version='1.0' encoding='UTF-8'?><request>"
                + "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in fields.items())
                + "</request>")
        headers = {**self._session(), "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        req = urllib.request.Request(self.base + path, body.encode(), headers)
        return self._parse(urllib.request.urlopen(req, timeout=TIMEOUT + 7).read())

    # ---------- состояние ----------

    def status(self):
        with self.lock:
            at, cached = self.cache
            if cached is not None and time.monotonic() - at < CACHE_SECONDS:
                return cached
            try:
                result = self._read_status()
            except (OSError, ET.ParseError, ModemError) as e:
                result = {"present": False, "error": str(e)}
            self.cache = (time.monotonic(), result)
            return result

    def _read_status(self):
        if self.device is None:
            info = self.get("/api/device/information")
            self.device = {"name": info.get("DeviceName"), "firmware": info.get("SoftwareVersion"),
                           "hardware": info.get("HardwareVersion")}
        st = self.get("/api/monitoring/status")
        gen, net = NETWORK_EX.get(st.get("CurrentNetworkTypeEx", ""),
                                  NETWORK_OLD.get(st.get("CurrentNetworkType", ""), (None, "?")))
        plmn = {}
        try:
            plmn = self.get("/api/net/current-plmn")
        except ModemError:
            pass  # без SIM или сети модем отвечает ошибкой -1, а остальное состояние читается
        traffic = self.get("/api/monitoring/traffic-statistics")
        signal = {}
        try:
            signal = self.get("/api/device/signal")
        except ModemError:
            pass  # у некоторых прошивок без входа не отдаётся
        month = {}
        try:
            month = self.get("/api/monitoring/month_statistics")
        except ModemError:
            pass
        mode = self.get("/api/net/net-mode").get("NetworkMode")
        return {
            "present": True,
            "device": self.device,
            "connection": CONNECTION.get(st.get("ConnectionStatus"), "error"),
            "connection_code": st.get("ConnectionStatus"),
            "sim": SIM.get(self.get("/api/pin/status").get("SimState"), "unknown"),
            "operator": plmn.get("FullName") or plmn.get("ShortName") or None,
            "generation": gen,
            "network": net,
            "bars": int(st.get("SignalIcon") or 0),
            "max_bars": int(st.get("maxsignal") or 5),
            "signal": {k: signal.get(k) for k in ("rsrp", "rsrq", "sinr", "rssi", "band") if signal.get(k)},
            "data": self.get("/api/dialup/mobile-dataswitch").get("dataswitch") == "1",
            "mode": next((name for name, code in MODES.items() if code == mode), mode),
            "rate": {"rx": int(traffic.get("CurrentDownloadRate") or 0),
                     "tx": int(traffic.get("CurrentUploadRate") or 0)},
            "session": {"rx": int(traffic.get("CurrentDownload") or 0),
                        "tx": int(traffic.get("CurrentUpload") or 0),
                        "seconds": int(traffic.get("CurrentConnectTime") or 0)},
            "month": {"rx": int(month.get("CurrentMonthDownload") or 0),
                      "tx": int(month.get("CurrentMonthUpload") or 0)} if month else None,
        }

    # ---------- управление ----------

    def _changed(self):
        self.cache = (0.0, None)  # следующий опрос — свежие данные

    def set_data(self, on):
        with self.lock:
            self.post("/api/dialup/mobile-dataswitch", {"dataswitch": 1 if on else 0})
            self._changed()

    def set_mode(self, name):
        if name not in MODES:
            raise ValueError(f"неизвестный режим {name}")
        with self.lock:
            cur = self.get("/api/net/net-mode")
            self.post("/api/net/net-mode", {"NetworkMode": MODES[name],
                                            "NetworkBand": cur.get("NetworkBand", "3FFFFFFF"),
                                            "LTEBand": cur.get("LTEBand", "7FFFFFFFFFFFFFFF")})
            self._changed()

    def reconnect(self):
        # В обычном (не daemon) потоке: если сервис перезапустят посреди переподключения,
        # Python перед выходом дождётся его, и резервный канал не останется выключенным.
        errors = []
        with self.lock:
            worker = threading.Thread(target=self._redial, args=(errors,), daemon=False,
                                      name="modem-reconnect")
            worker.start()
            worker.join()
            self._changed()
        if errors:
            raise errors[0]

    def _redial(self, errors):
        try:
            self.post("/api/dialup/mobile-dataswitch", {"dataswitch": 0})
        except Exception as e:  # ответ мог не дойти, а выключение — пройти: включаем в любом случае
            errors.append(e)
        for attempt in range(3):  # модем ещё рвёт соединение и может не ответить
            time.sleep(2)
            try:
                self.post("/api/dialup/mobile-dataswitch", {"dataswitch": 1})
                errors.clear()  # данные снова включены — значит, переподключение удалось
                return
            except Exception as e:
                if attempt == 2:
                    errors.append(e)

    def reboot(self):
        with self.lock:
            self.post("/api/device/control", {"Control": 1})
            self.device = None
            self._changed()


def default_routes():
    """Маршруты в интернет: [(интерфейс, метрика)], первый — основной."""
    try:
        out = subprocess.run(["ip", "-j", "route", "show", "default"], capture_output=True,
                             text=True, timeout=3).stdout
        routes = [(r.get("dev"), r.get("metric", 0)) for r in json.loads(out or "[]")]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return sorted(routes, key=lambda r: r[1])


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _ipv4(dev):
    try:
        info = json.loads(_run("ip", "-j", "-4", "addr", "show", "dev", dev) or "[]")
    except ValueError:
        return None
    return next((a["local"] for i in info for a in i.get("addr_info", []) if a.get("local")), None)


def local_access():
    """Как подключиться к Pi рядом: её Wi-Fi (точка доступа или клиент роутера) и кабель."""
    conn = _run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", WIFI)
    mode = ssid = clients = None
    if conn:
        wmode, ssid = (_run("nmcli", "-g", "802-11-wireless.mode,802-11-wireless.ssid",
                            "connection", "show", conn).splitlines() + ["", ""])[:2]
        mode = "hotspot" if wmode == "ap" else "client"
        if mode == "hotspot":
            clients = _run(IW, "dev", WIFI, "station", "dump").count("Station ")
    try:
        carrier = Path(f"/sys/class/net/{CABLE}/carrier").read_text().strip() == "1"
    except OSError:  # интерфейс выключен
        carrier = False
    return {"wifi": {"mode": mode, "ssid": ssid or None, "address": _ipv4(WIFI), "clients": clients},
            "cable": {"carrier": carrier, "address": _ipv4(CABLE)}}


def failover_state():
    try:
        state = json.loads(FAILOVER_STATE.read_text())
    except (OSError, ValueError):
        return None
    state["age"] = round(time.time() - state.get("time", 0), 1)
    return state


def internet(modem):
    routes = default_routes()
    return {
        "routes": [{"dev": dev, "metric": metric} for dev, metric in routes],
        "primary": routes[0][0] if routes else None,
        "failover": failover_state(),
        "modem": modem.status(),
        "local": local_access(),
    }
