"""AdGuard Home для панели: статистика блокировок и пауза защиты.

AdGuard Home (~/project/adguard) отвечает на DNS для устройств в Wi-Fi Pi, а свой веб держит
на 127.0.0.1:3080 под паролем из adguard.env. Панель ходит в его API сама и пускает браузер
через свой порт (45463, panel.AdGuardProxyHandler), подставляя пароль, — второго логина нет.
"""
import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("webcam")

PORT = 3080
API = f"http://127.0.0.1:{PORT}/control"
ENV = Path.home() / "project/adguard/adguard.env"
CACHE_SECONDS = 5
PAUSES = (10, 60)  # на сколько минут можно выключить защиту из панели
TOP = 6


def auth_header():
    """«Basic …» из adguard.env (ADGUARD_AUTH=логин:пароль); None — файла нет."""
    try:
        for line in ENV.read_text().splitlines():
            if line.startswith("ADGUARD_AUTH="):
                return "Basic " + base64.b64encode(line.split("=", 1)[1].strip().encode()).decode()
    except OSError:
        pass
    return None


def _top(items):
    """[{ключ: число}, …] из статистики AdGuard → [(ключ, число)], первые TOP."""
    return [pair for item in (items or [])[:TOP] for pair in item.items()]


class AdGuard:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = (0.0, None)

    def _api(self, path, body=None):
        req = urllib.request.Request(API + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode())
        if auth := auth_header():
            req.add_header("Authorization", auth)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError:
            return None  # часть команд отвечает просто «OK»

    # ---------- состояние ----------

    def status(self):
        with self.lock:
            at, cached = self.cache
            if cached is None or time.monotonic() - at >= CACHE_SECONDS:
                cached = self._read()
                self.cache = (time.monotonic(), cached)
            return cached

    def _read(self):
        try:
            st, stats, clients = self._api("/status"), self._api("/stats"), self._api("/clients")
        except urllib.error.HTTPError as e:
            return {"available": False, "error": "неверный пароль в adguard.env" if e.code in (401, 403)
                    else f"AdGuard Home ответил {e.code}"}
        except (OSError, ValueError):
            return {"available": False, "error": "AdGuard Home не отвечает"}
        # Имена: свои клиенты AdGuard, затем найденные им самим (обратный DNS к dnsmasq, ARP).
        names = {c["ip"]: c["name"] for c in (clients or {}).get("auto_clients") or [] if c.get("name")}
        for c in (clients or {}).get("clients") or []:
            for client_id in c.get("ids") or []:
                names[client_id] = c.get("name")
        for local in ("127.0.0.1", "::1"):
            names.setdefault(local, "сама Pi")
        blocked = sum(stats.get(k) or 0 for k in
                      ("num_blocked_filtering", "num_replaced_safebrowsing", "num_replaced_parental"))
        return {
            "available": True,
            "version": st.get("version"),
            "running": bool(st.get("running")),
            "protection": bool(st.get("protection_enabled")),
            "paused_ms": st.get("protection_disabled_duration") or 0,  # сколько ещё длится пауза
            "queries": stats.get("num_dns_queries") or 0,
            "blocked": blocked,
            "avg_ms": round((stats.get("avg_processing_time") or 0) * 1000, 1),
            "hours": stats.get("dns_queries") or [],  # по часам, последний — текущий
            "hours_blocked": stats.get("blocked_filtering") or [],
            "top_blocked": [{"domain": d, "count": n} for d, n in _top(stats.get("top_blocked_domains"))],
            "top_clients": [{"ip": ip, "name": names.get(ip), "count": n} for ip, n in _top(stats.get("top_clients"))],
        }

    # ---------- команды ----------

    def command(self, action, minutes=None):
        """pause (minutes из PAUSES) или resume; ValueError — с текстом для страницы."""
        if action == "pause":
            if minutes not in PAUSES:
                raise ValueError(f"пауза — {' или '.join(map(str, PAUSES))} минут")
            body = {"enabled": False, "duration": minutes * 60_000}
        elif action == "resume":
            body = {"enabled": True}
        else:
            raise ValueError("неизвестная команда")
        try:
            self._api("/protection", body)
        except (OSError, ValueError):
            raise ValueError("AdGuard Home не отвечает") from None
        log.info("AdGuard: %s", "защита включена" if action == "resume" else f"пауза на {minutes} мин")
        self.cache = (0.0, None)
