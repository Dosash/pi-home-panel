#!/usr/bin/env python3
"""Панель zapret: включение, выбор и проверка стратегии, подбор, свои списки, обновление стратегий.

Открывается кнопкой «Настройка zapret» в шапке главной панели (порт 45464, отдельное окно).
Без входа, как и главная панель: видна только в локальной сети. Отвечает только на свой IP
и локальные имена (защита от DNS rebinding — общая с панелью), команды — только JSON.
Долгие дела (проверка, подбор, применение, обновление) идут в фоне по одному; их ход — в job.
"""
import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import checker
import control

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webcam"))
import panel  # noqa: E402  (проверка Host — та же, что у главной панели)

log = logging.getLogger("zapret-panel")

HERE = Path(__file__).resolve().parent
PAGE = HERE / "page.html"
STATE = HERE / "state.json"  # итоги проверок и подбора — в .gitignore
FLOWSEAL_API = "https://api.github.com/repos/flowseal/zapret-discord-youtube/releases/latest"
MAX_BODY = 256 * 1024  # свои списки бывают длинными
VERSION = re.compile(r"^[0-9][0-9A-Za-z.\-]{0,31}$")
SERVICES = {"Discord": "Discord", "YouTube": "YouTube", "Google": "Google", "Cloudflare": "Cloudflare"}


# ---------- состояние ----------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.data = json.loads(STATE.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.job = None  # {"kind", "state": running|done|failed, "progress", "total", "text", ...}
        self.cancel = threading.Event()

    def save(self, **changes):
        with self.lock:
            self.data.update(changes)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False))
            tmp.replace(STATE)

    def start(self, kind, target, *args):
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise ValueError("уже идёт: " + self.job["title"])
            self.cancel.clear()
            self.job = {"kind": kind, "title": TITLES[kind], "state": "running", "progress": 0, "total": 0,
                        "text": "", "started": time.time()}
        threading.Thread(target=self._run, args=(target, *args), daemon=True, name=kind).start()

    def _run(self, target, *args):
        try:
            result = target(*args) or {}
            self.job.update(state="done", finished=time.time(), **result)
        except Exception as e:  # покажем в панели как есть
            log.exception("%s не удалось", self.job["kind"])
            self.job.update(state="failed", error=str(e), finished=time.time())


TITLES = {"apply": "применение", "check": "проверка", "check-strategy": "проверка стратегии",
          "sweep": "подбор стратегии", "update": "обновление стратегий"}
state = State()


def summary(rows):
    """{"Discord": "ok"|"partial"|"blocked", ...} — по целям с таким префиксом в имени."""
    out = {}
    for service in SERVICES:
        checks = [row[t]["status"] for row in rows if row["name"].startswith(service)
                  for t in checker.TLS if t in row]
        if checks:
            ok = sum(c == "ok" for c in checks)
            out[service] = "ok" if ok == len(checks) else "partial" if ok else "blocked"
    return out


def result(rows):
    return {"rows": rows, "score": checker.score(rows), "total": checker.total(rows), "summary": summary(rows),
            "time": time.time()}


# ---------- дела ----------

def do_apply(settings):
    state.job["text"] = "перезапускаю zapret…" if settings["enabled"] else "выключаю zapret…"
    control.apply(settings)
    return {"text": "готово"}


def do_check():
    state.job["text"] = "проверяю, что открывается сейчас — как у устройств в Wi-Fi…"
    res = result(checker.run_suite())
    res["strategy"] = control.load_settings()["strategy"] if control.service_state()["active"] == "active" else None
    state.save(check=res)
    return {"text": f"{res['score']} из {res['total']}"}


def do_check_strategy(name):
    strategy = find_strategy(name)
    state.job["text"] = f"проверяю «{name}» отдельно от Wi-Fi…"
    res = result(checker.check_strategy(strategy))
    scores = state.data.get("scores", {})
    scores[name] = {k: res[k] for k in ("score", "total", "summary", "time")}
    state.save(scores=scores)
    return {"text": f"«{name}»: {res['score']} из {res['total']}", "rows": res["rows"]}


def do_sweep():
    found = [s for s in control.load_strategies(control.load_settings()["game"]) if "error" not in s]
    state.job["total"] = len(found)
    scores = {}
    for i, strategy in enumerate(found):
        if state.cancel.is_set():
            break
        state.job.update(progress=i, text=f"«{strategy['name']}» — {i + 1} из {len(found)}")
        res = result(checker.check_strategy(strategy))
        scores[strategy["name"]] = {k: res[k] for k in ("score", "total", "summary", "time")}
        state.save(scores={**state.data.get("scores", {}), **scores})
    best = max(scores.items(), key=lambda kv: kv[1]["score"], default=(None, None))[0]
    state.save(best=best, swept=time.time())
    state.job["progress"] = len(scores)
    stopped = " (остановлен)" if state.cancel.is_set() else ""
    return {"text": f"лучшая: «{best}»{stopped}" if best else "ничего не проверено", "best": best}


def latest_release():
    req = urllib.request.Request(FLOWSEAL_API, headers={"Accept": "application/vnd.github+json",
                                                        "User-Agent": "pi-home-panel"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    tag = str(data.get("tag_name", ""))
    if not VERSION.match(tag):
        raise ValueError(f"странная версия на GitHub: {tag!r}")
    asset = next((a for a in data.get("assets", []) if a.get("name") == f"zapret-discord-youtube-{tag}.tar.gz"), None)
    return {"version": tag, "url": asset and asset["browser_download_url"], "size": asset and asset.get("size"),
            "checked": time.time()}


def do_update():
    """Новый релиз flowseal: скачать, взять стратегии, списки и заготовки (.exe не нужны), переключить."""
    info = latest_release()
    state.save(latest=info)
    version = info["version"]
    if version == control.release_version():
        return {"text": f"уже последняя: {version}"}
    if not info["url"] or not info["url"].startswith("https://github.com/flowseal/"):
        raise ValueError("в релизе нет архива tar.gz")
    state.job["text"] = f"скачиваю {version} ({round((info['size'] or 0) / 1e6, 1)} МБ)…"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "release.tar.gz"
        with urllib.request.urlopen(info["url"], timeout=60) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 16)
        out = Path(tmp) / "x"
        with tarfile.open(archive) as tar:
            members = [m for m in tar.getmembers() if (m.isfile() or m.isdir())
                       and not m.name.startswith("/") and ".." not in Path(m.name).parts
                       and not m.name.lower().endswith((".exe", ".dll", ".sys"))]
            tar.extractall(out, members=members, filter="data")
        root = next((p for p in out.iterdir() if p.is_dir()), out)
        if not list(root.glob("general*.bat")):
            raise ValueError("в архиве нет стратегий general*.bat")
        state.job["text"] = "проверяю стратегии новой версии…"
        dest = control.FLOWSEAL / version
        control.sudo("rm", "-rf", str(dest))
        control.sudo("cp", "-r", str(root), str(dest))
        control.sudo("chmod", "-R", "a+rX", str(dest))
    previous = control.RELEASE.resolve()
    control.sudo("ln", "-sfn", version, str(control.RELEASE))
    settings = control.load_settings()
    names = [s["name"] for s in control.load_strategies(settings["game"]) if "error" not in s]
    if settings["strategy"] not in names:  # стратегию убрали в новой версии — откатываемся
        control.sudo("ln", "-sfn", previous.name, str(control.RELEASE))
        raise ValueError(f"в {version} нет стратегии «{settings['strategy']}» — оставил {previous.name}")
    state.job["text"] = "применяю…"
    control.apply(settings)
    state.save(scores={}, best=None)  # старые итоги к новым стратегиям не относятся
    return {"text": f"обновлено: {previous.name} → {version}"}


def find_strategy(name):
    found = {s["name"]: s for s in control.load_strategies(control.load_settings()["game"])}
    strategy = found.get(name)
    if strategy is None:
        raise ValueError(f"нет стратегии «{name}»")
    if "error" in strategy:
        raise ValueError(strategy["error"])
    return strategy


# ---------- HTTP ----------

def status():
    settings = control.load_settings()
    return {
        "service": control.service_state(),
        "settings": settings,
        "versions": {"zapret": control.zapret_version(), "flowseal": control.release_version()},
        "job": state.job,
        "check": state.data.get("check"),
        "scores": state.data.get("scores", {}),
        "best": state.data.get("best"),
        "swept": state.data.get("swept"),
        "latest": state.data.get("latest"),
    }


def command(data):
    action = str(data.get("action") or "")
    names = [s["name"] for s in control.load_strategies()]
    if action == "apply":
        state.start("apply", do_apply, control.validate(data.get("settings") or {}, names))
    elif action == "lists":
        lists = data.get("lists") or {}
        for name in control.strategies.USER_LISTS:
            if name in lists:
                control.write_list(name, lists[name])
        state.start("apply", do_apply, control.load_settings())
    elif action == "check":
        state.start("check", do_check)
    elif action == "check-strategy":
        find_strategy(str(data.get("name") or ""))
        state.start("check-strategy", do_check_strategy, str(data["name"]))
    elif action == "sweep":
        state.start("sweep", do_sweep)
    elif action == "cancel":
        state.cancel.set()
    elif action == "update-check":
        state.save(latest=latest_release())
    elif action == "update":
        state.start("update", do_update)
    else:
        raise ValueError(f"неизвестное действие: {action}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def send_body(self, body, ctype, code=HTTPStatus.OK):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code=HTTPStatus.OK):
        self.send_body(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", code)

    def allowed(self):
        if panel.host_allowed(self):
            return True
        self.send_body("Откройте по адресу Pi: http://10.20.0.1:45464 или http://pi.lan:45464".encode(),
                       "text/plain; charset=utf-8", HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self):
        if not self.allowed():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                self.send_body(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self.send_json(status())
            elif path == "/api/strategies":
                game = control.load_settings()["game"]
                self.send_json([{**s, "score": state.data.get("scores", {}).get(s["name"])}
                                for s in control.load_strategies(game)])
            elif path == "/api/lists":
                self.send_json({name: control.read_list(name) for name in control.strategies.USER_LISTS})
            elif path == "/api/log":
                self.send_body(control.journal(60).encode(), "text/plain; charset=utf-8")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.allowed():
            return
        if self.path != "/api/action":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        # Только JSON: обычная форма с чужого сайта так не отправит.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_json({"error": "json required"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.send_json({"error": "слишком большой запрос"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("ожидается объект JSON")
            command(data)
        except (ValueError, RuntimeError, OSError) as e:
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"ok": True}, HTTPStatus.ACCEPTED)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=45464)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    checker.clear_rules()  # после сбоя могли остаться временные правила проверки
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log.info("панель zapret: http://%s:%d", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
