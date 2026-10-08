"""Панель: проксирование Zigbee2MQTT и Home Assistant за общим входом, состояние сервисов."""
import ipaddress
import json
import logging
import select
import socket
import subprocess
import time
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

import adguard
from auth import COOKIE

log = logging.getLogger("webcam")

Z2M_PORT = 8099  # интерфейс Zigbee2MQTT, слушает только 127.0.0.1
# Какие USB-устройства — Zigbee-адаптеры, знает zigbee-herdsman; adapters.py достаёт это из него.
ZIGBEE_ADAPTERS = Path.home() / "project/zigbee/adapters.py"
HA_PORT = 8123  # Home Assistant, слушает только 127.0.0.1
ZAPRET_PANEL_PORT = 45464  # своя панель zapret, ~/project/zapret
ZAPRET_SETTINGS = Path.home() / "project/zapret/settings.json"

# Заголовки одного соединения: дальше прокси их не передаёт.
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def port_open(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def service_state(name):
    try:
        return subprocess.run(["systemctl", "is-active", name], capture_output=True,
                              text=True, timeout=3).stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


_adapter_cache = (0.0, False)


def zigbee_adapter():
    """Вставлен ли Zigbee-адаптер, который найдёт Zigbee2MQTT (модемы и прочие USB-UART — нет).

    Проверка может запускать node (~1 с CPU), поэтому результат держим 30 секунд.
    """
    global _adapter_cache
    at, found = _adapter_cache
    if time.monotonic() - at < 30:
        return found
    try:
        found = subprocess.run(["python3", str(ZIGBEE_ADAPTERS), "--check"], capture_output=True,
                               timeout=40).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        found = False
    _adapter_cache = (time.monotonic(), found)
    return found


def overview():
    return {
        "zigbee": {
            "running": port_open(Z2M_PORT),
            "service": service_state("zigbee2mqtt"),
            "adapter": zigbee_adapter(),
        },
        "homeassistant": {"running": port_open(HA_PORT)},
        "zapret": zapret(),
    }


def zapret():
    """Обход блокировок для кнопки в шапке: служба, панель и выбранная стратегия."""
    try:
        settings = json.loads(ZAPRET_SETTINGS.read_text())
    except (OSError, ValueError):
        settings = {}
    return {"service": service_state("zapret"), "panel": port_open(ZAPRET_PANEL_PORT),
            "enabled": settings.get("enabled", True), "strategy": settings.get("strategy")}


def request_host(handler):
    host = handler.headers.get("Host", "localhost")
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(handler):
    """Защита от DNS rebinding: панель отвечает только на свой IP и локальные имена.

    Без входа чужой сайт мог бы подсунуть браузеру своё имя с адресом Pi и управлять панелью
    как своей страницей. Такой запрос придёт с чужим именем в Host — его не пускаем."""
    host = urlsplit("//" + handler.headers.get("Host", "")).hostname
    if not host:  # без Host ходят только скрипты, браузер его шлёт всегда
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    host = host.rstrip(".")
    return host in ("localhost", socket.gethostname().lower()) or host.endswith((".local", ".lan", ".localhost"))


def forbid_host(handler):
    body = ("Откройте панель по адресу Pi: http://10.20.0.1:45461 (Wi-Fi), "
            "http://10.10.0.1:45461 (кабель) или http://pi.lan:45461.").encode()
    handler.send_response(HTTPStatus.FORBIDDEN)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def panel_url(handler, panel_port):
    return f"http://{request_host(handler)}:{panel_port}"


def ha_url(handler, ha_port):
    return f"http://{request_host(handler)}:{ha_port}/"


def session_token(handler):
    try:
        cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    except CookieError:
        return None
    morsel = cookie.get(COOKIE)
    return morsel.value if morsel else None


def logged_in(handler, auth):
    if not auth.enabled:
        return True
    return auth.valid(session_token(handler)) or auth.basic_ok(
        handler.headers.get("Authorization", ""))


def _strip_session(cookie_header):
    """Наш токен сессии не нужен сервисам за прокси — не отдаём его им."""
    parts = [p for p in cookie_header.split(";") if p.strip().split("=", 1)[0] != COOKIE]
    return ";".join(parts).strip()


def _pipe(a, b):
    """Гоняет байты в обе стороны, пока одна из сторон не закроет соединение (WebSocket)."""
    socks = [a, b]
    while True:
        ready, _, _ = select.select(socks, [], [], 300)
        for s in ready:
            data = s.recv(65536)
            if not data:
                return
            (b if s is a else a).sendall(data)


def proxy(handler, port, extra_headers=None):
    """Передаёт запрос сервису на 127.0.0.1:port. False, если сервис не отвечает.
    extra_headers — свои заголовки для сервиса (например, его пароль)."""
    try:
        up = socket.create_connection(("127.0.0.1", port), timeout=5)
    except OSError:
        return False
    upgrade = handler.headers.get("Upgrade", "").lower() == "websocket"
    lines = [f"{handler.command} {handler.path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    for key, value in handler.headers.items():
        low = key.lower()
        if low == "host" or (low in HOP_HEADERS and not upgrade):
            continue
        if low == "authorization" and value.startswith("Basic "):
            continue  # это наш логин панели; токены самого сервиса (Bearer) передаём
        if low.startswith("x-forwarded-") or low == "forwarded":
            continue  # HA на X-Forwarded-For от неизвестного прокси отвечает 400
        if low == "cookie":
            value = _strip_session(value)
            if not value:
                continue
        lines.append(f"{key}: {value}")
    lines += extra_headers or []
    if upgrade:
        lines += ["Connection: Upgrade", "Upgrade: websocket"]
    else:
        lines.append("Connection: close")
    body = b""
    length = int(handler.headers.get("Content-Length") or 0)
    if length:
        body = handler.rfile.read(length)
    handler.close_connection = True
    client = handler.connection
    try:
        up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        up.settimeout(None)
        if upgrade:
            _pipe(client, up)
        else:
            while data := up.recv(65536):
                client.sendall(data)
    except OSError:
        pass
    finally:
        up.close()
    return True


def unavailable_page(title, text):
    """Страница-заглушка, которая сама обновляется, пока сервис не поднимется."""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5"><title>{title}</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:16px;
         background:#0f1115; color:#e6e8eb; font:15px/1.5 system-ui,sans-serif; text-align:center; }}
  a {{ color:#58a6ff; }} p {{ color:#8b929c; max-width:460px; }}
</style></head><body><div>
<h2>{title}</h2><p>{text}</p><p><a href="/">← на главную</a></p>
</div></body></html>""".encode()


class ServiceProxyHandler(BaseHTTPRequestHandler):
    """Свой порт для сервиса за панелью: пускает только вошедших в панель и проксирует всё в него.

    HA и AdGuard Home не умеют жить в подпапке, поэтому у каждого свой порт. Cookie не зависят
    от порта, так что сессия из панели действует и здесь.
    """

    auth = None
    panel_port = 45461
    target_port = None
    login_next = ""  # куда вернуть после входа в панель
    starting = ("", "")  # заголовок и текст, пока сервис не отвечает

    def log_message(self, fmt, *args):
        log.debug("%s %s %s", self.login_next, self.address_string(), fmt % args)

    def extra_headers(self):
        return None

    def handle_any(self):
        if not host_allowed(self):
            forbid_host(self)
            return
        if not logged_in(self, self.auth):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"{panel_url(self, self.panel_port)}/login?next={self.login_next}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not proxy(self, self.target_port, self.extra_headers()):
            body = unavailable_page(*self.starting)
            body = body.replace(b'href="/"', f'href="{panel_url(self, self.panel_port)}/"'.encode())
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any


class HAProxyHandler(ServiceProxyHandler):
    """Порт Home Assistant. Запросы приходят с 127.0.0.1 — HA пускает их без своего логина."""

    target_port = HA_PORT
    login_next = "ha"
    starting = ("Home Assistant запускается…", "Первый запуск занимает пару минут. Страница обновится сама.")


class AdGuardProxyHandler(ServiceProxyHandler):
    """Порт AdGuard Home: пароль от его веба подставляем сами — второго логина нет."""

    target_port = adguard.PORT
    login_next = "adguard"
    starting = ("AdGuard Home не отвечает", "Проверьте на Pi: sudo systemctl status AdGuardHome")

    def extra_headers(self):
        auth = adguard.auth_header()
        return [f"Authorization: {auth}"] if auth else None
