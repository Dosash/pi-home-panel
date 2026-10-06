"""ZeroTier для панели: узел, сеть, подключённые устройства и трафик.

Данные — из локального API службы zerotier-one (127.0.0.1:9993). Ему нужен токен; для запуска
не от root ZeroTier штатно берёт его из ~/.zeroTierOneAuthToken (копия authtoken.secret, права 600).
"""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:9993"
TOKEN = Path.home() / ".zeroTierOneAuthToken"
CACHE_SECONDS = 3


class ZeroTier:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = (0.0, None)
        self.traffic = None  # (время, rx, tx) прошлого замера — для скорости

    def _get(self, path):
        req = urllib.request.Request(API + path, headers={"X-ZT1-Auth": TOKEN.read_text().strip()})
        return json.load(urllib.request.urlopen(req, timeout=3))

    def status(self):
        with self.lock:
            at, cached = self.cache
            if cached is not None and time.monotonic() - at < CACHE_SECONDS:
                return cached
            try:
                result = self._read()
            except (OSError, ValueError) as e:
                result = {"present": False, "error": str(e)}
            self.cache = (time.monotonic(), result)
            return result

    def _read(self):
        node = self._get("/status")
        networks = self._get("/network")
        peers = self._get("/peer")
        # Контроллер сети (my.zerotier.com) — тоже «узел», но не устройство: его адрес = начало id сети.
        controllers = {n["id"][:10] for n in networks}
        devices = []
        for p in peers:
            if p.get("role") != "LEAF" or p["address"] in controllers:
                continue
            paths = [x for x in p.get("paths", []) if x.get("active") and not x.get("expired")]
            preferred = next((x for x in paths if x.get("preferred")), paths[0] if paths else None)
            devices.append({
                "address": p["address"],
                "latency": p.get("latency") if (p.get("latency") or -1) >= 0 else None,
                # Нет своего пути — пакеты идут через корневые серверы ZeroTier (медленнее).
                "direct": bool(paths),
                "path": preferred["address"].split("/")[0] if preferred else None,
            })
        return {
            "present": True,
            "address": node.get("address"),
            "version": node.get("version"),
            "online": bool(node.get("online")),
            "tcp_fallback": bool(node.get("tcpFallbackActive")),
            "roots": sum(1 for p in peers if p.get("role") == "PLANET"
                         and any(x.get("active") for x in p.get("paths", []))),
            "networks": [{
                "id": n["id"],
                "name": n.get("name") or n["id"],
                "status": n.get("status"),
                "ips": [a.split("/")[0] for a in n.get("assignedAddresses", []) if ":" not in a],
                "device": n.get("portDeviceName"),
            } for n in networks],
            "devices": sorted(devices, key=lambda d: d["address"]),
            "traffic": self._traffic([n.get("portDeviceName") for n in networks]),
        }

    def _traffic(self, devices):
        rx = tx = 0
        for dev in filter(None, devices):
            base = Path("/sys/class/net") / dev / "statistics"
            try:
                rx += int((base / "rx_bytes").read_text())
                tx += int((base / "tx_bytes").read_text())
            except OSError:
                pass
        now = time.monotonic()
        rate = {"rx": 0, "tx": 0}
        if self.traffic:
            t0, rx0, tx0 = self.traffic
            if now > t0 and rx >= rx0 and tx >= tx0:
                rate = {"rx": (rx - rx0) / (now - t0), "tx": (tx - tx0) / (now - t0)}
        self.traffic = (now, rx, tx)
        return {"rx": rx, "tx": tx, "rate": rate}
