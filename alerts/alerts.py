#!/usr/bin/env python3
"""SMS-оповещения через LTE-модем: пропадало питание, холодно у датчика, движение у камеры.

Что включено и на какие номера — settings.json (sms_settings.py), его меняет панель, раздел «SMS».
Сервис перечитывает файл сам, перезапускать не нужно.

Питание. Пока Pi работает, сервис раз в минуту отмечает в state.json, что жив, а при штатном
выключении (poweroff, reboot — systemd останавливает сервисы) записывает «выключились чисто».
Нет такой отметки после загрузки — питание пропало внезапно: последняя отметка «жив» ≈ когда
пропал свет, загрузка ≈ когда он вернулся. Своих часов у Pi нет: время верное только после
синхронизации по NTP через модем, поэтому это SMS ждёт её (не дольше SYNC_WAIT).

Температура. Датчики Zigbee — из MQTT (Zigbee2MQTT), Bluetooth — из панели (она расшифровывает
рекламу BLE-датчиков). Холоднее порога дольше TEMP_HOLD — SMS; потеплело на TEMP_HYST выше
порога — SMS, что отпустило. Датчик молчит дольше заданного (села батарейка) — тоже SMS.
Все замеченные датчики с температурой сервис пишет в state.json — из них выбирают в панели.

Движение. Панель публикует в MQTT webcam/motion; на каждое начало движения — SMS, но не чаще
заданной паузы.

Не ушедшие SMS (нет SIM, нет сети) лежат в state.json и повторяются, в том числе после
следующих загрузок.
"""
import argparse
import json
import logging
import os
import queue
import re
import signal
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import sms_settings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webcam"))
from modem import HiLink, ModemError  # noqa: E402  (общий с панелью клиент модема)

STATE = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/sms-alerts")) / "state.json"
SYNCED = Path("/run/systemd/timesync/synchronized")  # systemd-timesyncd создаёт после NTP
BEAT_EVERY = 60  # сек — с такой точностью известно, когда пропал свет
SYNC_WAIT = 10 * 60  # дольше точного времени не ждём — шлём, что знаем
RETRY = (30, 60, 120, 300, 600)  # паузы между попытками отправить, дальше — последняя
OUTBOX_MAX = 20  # SIM нет неделями — копим не больше стольких SMS, старые выбрасываем
LOG_MAX = 20  # последние отправленные — для панели
SEEN_MAX = 40  # замеченных датчиков с температурой (соседские BLE тоже попадают)

TEMP_HOLD = 5 * 60  # сек холоднее порога, прежде чем слать: не из-за одного замера
TEMP_HYST = 1.0  # °C — «потеплело», только когда выше порога на столько: без SMS туда-сюда
PANEL_BT = "http://127.0.0.1:45461/api/bluetooth"
BT_EVERY = 60  # сек — как часто спрашивать панель о BLE-датчиках
MQTT_HOST, MQTT_PORT = "localhost", 1883
Z2M, MOTION_TOPIC = "zigbee2mqtt", "webcam/motion"  # см. ../zigbee и ../webcam/ha_mqtt.py

log = logging.getLogger("sms-alerts")


# ---------- текст ----------

def when(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def duration(seconds):
    m = max(1, round(seconds / 60))
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} ч {m} мин" if m else f"{h} ч"
    d, h = divmod(h, 24)
    return f"{d} д {h} ч" if h else f"{d} д"


def deg(v):
    s = f"{v:+.1f}".replace(".", ",").replace("-", "−")
    return ("0" if s in ("+0,0", "−0,0") else s.removesuffix(",0")) + " °C"


def times(n):
    return "раза" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "раз"


# ---------- питание ----------

def on_start(state, boot, now, exact):
    """Состояние после запуска сервиса: прошлая загрузка без чистого выключения — в pending."""
    pending = list(state.get("pending", []))
    if state.get("boot") not in (None, boot) and not state.get("clean"):
        pending.append({"boot": boot, "off": state.get("alive"), "off_exact": bool(state.get("alive_exact"))})
    # та же загрузка — сервис просто перезапустили, ничего не случилось
    return {"outbox": [], "log": [], "sensors": {}, **state,
            "boot": boot, "clean": False, "alive": now, "alive_exact": exact, "pending": pending}


def note_power_on(state, boot, now, uptime):
    """Когда часы уже точные: включились = сейчас минус сколько работаем."""
    for p in state["pending"]:
        if p["boot"] == boot and p.get("on") is None:
            p["on"] = now - uptime


def outage(p):
    """« 07.10 14:32–15:10 (38 мин)» — насколько известно."""
    if p.get("off") is None:
        return ""
    off = when(p["off"])
    mark = "" if p["off_exact"] else "≈"  # часы и до отключения не были синхронизированы
    if p.get("on") is None:
        return f" с {mark}{off}"
    on = when(p["on"])
    if on[:5] == off[:5]:
        on = on[6:]  # тот же день — только время
    length = f" ({duration(p['on'] - p['off'])})" if p["off_exact"] else ""
    return f" {mark}{off}–{on}{length}"


def power_message(pending):
    # Кириллица — 70 символов на SMS: «Pi: пропадало питание 07.10 14:32–15:10 (38 мин). Работает.»
    n = len(pending)
    head = "Pi: пропадало питание" if n == 1 else f"Pi: питание пропадало {n} {times(n)}, последний"
    return f"{head}{outage(pending[-1])}. Работает."


# ---------- температура и движение ----------

class Thermo:
    """Один датчик: копит показания; check() отдаёт тексты SMS, когда пора. Время — monotonic."""

    def __init__(self, label, ident, low, stale, now):
        self.label, self.ident, self.low, self.stale = label, ident.lower(), low, stale
        self.value, self.seen = None, now  # молчание считаем от запуска
        self.below_since = None
        self.cold = self.silent = False  # о чём уже сообщили

    def matches(self, *ids):
        return any(i and i.lower() == self.ident for i in ids)

    def reading(self, value, now):
        self.value, self.seen = value, now
        if value >= self.low:
            self.below_since = None
        elif self.below_since is None:
            self.below_since = now

    def check(self, now, stamp=""):
        out = []
        quiet = now - self.seen
        if self.silent and quiet < self.stale:
            self.silent = False
            out.append(f"Pi: датчик {self.label} снова на связи, {deg(self.value)}{stamp}.")
        elif not self.silent and self.stale and quiet >= self.stale:
            self.silent = True
            last = f", последний раз {deg(self.value)}" if self.value is not None else ""
            out.append(f"Pi: датчик {self.label} молчит {duration(quiet)}{last}{stamp}.")
        if not self.cold and self.below_since is not None and now - self.below_since >= TEMP_HOLD:
            self.cold = True
            out.append(f"Pi: холодно — {self.label} {deg(self.value)} (порог {deg(self.low)}){stamp}.")
        elif self.cold and self.value >= self.low + TEMP_HYST:
            self.cold = False
            out.append(f"Pi: {self.label} снова {deg(self.value)}{stamp}.")
        return out


def sync_thermos(thermos, settings, now):
    """Датчики из настроек; уже следившие сохраняют, что знали (иначе после правки — повторные SMS)."""
    t = settings["temperature"]
    if not t["enabled"]:
        return []
    old = {th.ident: th for th in thermos}
    out = []
    for s in t["sensors"]:
        th = old.get(s["id"].lower()) or Thermo(s["label"], s["id"], t["min"], 0, now)
        th.label, th.low, th.stale = s["label"], t["min"], t["stale"] * 60
        out.append(th)
    return out


class Pause:
    """Не чаще раза в pause секунд."""

    def __init__(self):
        self.last = None

    def ready(self, now, pause):
        if self.last is not None and now - self.last < pause:
            return False
        self.last = now
        return True


def mqtt_listener(events):
    """Температуры от Zigbee2MQTT и начала движения от панели — в очередь events."""
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            client.subscribe([(f"{Z2M}/#", 0), (MOTION_TOPIC, 0)])

    def on_message(client, userdata, msg):
        if msg.topic == MOTION_TOPIC:
            # retained ON при подключении — это не новое движение
            if msg.payload == b"ON" and not msg.retain:
                events.put(("motion",))
            return
        name = msg.topic.split("/", 1)[1]
        if name.startswith("bridge/") or name.rsplit("/", 1)[-1] in ("set", "get", "availability"):
            return
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        if isinstance(data, dict) and isinstance(data.get("temperature"), (int, float)):
            events.put(("temp", "zigbee", name, name, float(data["temperature"])))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sms-alerts")
    client.on_connect, client.on_message = on_connect, on_message
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()  # сам переподключается, если брокер перезапустят
    return client


def bluetooth_readings():
    """[(адрес, имя, °C)] — BLE-датчики, которые сейчас видит панель."""
    try:
        with urllib.request.urlopen(PANEL_BT, timeout=5) as r:
            devices = json.load(r).get("devices", [])
    except (OSError, ValueError):
        return []
    out = []
    for d in devices:
        t = ((d.get("sensor") or {}).get("values") or {}).get("temperature")
        if isinstance(t, (int, float)) and d.get("address"):
            out.append((d["address"], d.get("name"), float(t)))
    return out


def remember(seen, source, ident, name, value, now):
    """Замеченный датчик — для выбора в панели. Самые давние вытесняются."""
    seen[ident] = {"name": name, "source": source, "value": value, "at": now}
    if len(seen) > SEEN_MAX:
        del seen[min(seen, key=lambda k: seen[k]["at"])]


# ---------- отправка ----------

def same_number(a, b):
    return re.sub(r"\D", "", a)[-10:] == re.sub(r"\D", "", b)[-10:]


def send_next(modem, state, phones):
    """Первое SMS из очереди — тем, кому ещё не ушло. True — дошло до всех, убрано из очереди."""
    item = state["outbox"][0]
    left = [p for p in phones if not any(same_number(p, s) for s in item["sent_to"])]
    ok, failed = modem.send_sms(left, item["text"])
    item["sent_to"] += [p for p in left if any(same_number(p, s) for s in ok)]
    if len(item["sent_to"]) < len(phones):
        raise ModemError("не ушло на " + (", ".join(failed) or "часть номеров"))
    state["outbox"].pop(0)
    state["log"] = (state.get("log", []) + [{"text": item["text"], "at": time.time(), "to": item["sent_to"]}])[-LOG_MAX:]
    log.info("SMS отправлено: %s", item["text"])
    return True


# ---------- сервис ----------

def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def uptime():
    return float(Path("/proc/uptime").read_text().split()[0])


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(state):
    tmp = STATE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())  # отметка должна пережить внезапное отключение
    os.replace(tmp, STATE)


class Service:
    def __init__(self):
        self.stop = False
        self.boot, self.started = boot_id(), time.monotonic()
        self.state = on_start(load(), self.boot, time.time(), SYNCED.exists())
        save(self.state)
        if self.state["pending"]:
            log.warning("Pi выключалась аварийно: %s", power_message(self.state["pending"]))
        self.settings, self.settings_mtime = None, None
        self.thermos, self.motion = [], Pause()
        self.events = queue.Queue()
        self.modem = HiLink()
        self.next_beat = self.next_bt = self.next_try = 0.0
        self.tries = 0

    def reload(self, mono):
        try:
            mtime = sms_settings.PATH.stat().st_mtime
        except OSError:
            mtime = None
        if self.settings is not None and mtime == self.settings_mtime:
            return
        self.settings, self.settings_mtime = sms_settings.load(), mtime
        self.thermos = sync_thermos(self.thermos, self.settings, mono)
        s = self.settings
        log.info("настройки: номера %s; питание %s, температура %s, движение %s",
                 ", ".join(s["phones"]) or "не заданы", *("вкл" if s[k]["enabled"] else "выкл"
                                                          for k in ("power", "temperature", "motion")))

    def queue_sms(self, text):
        if not self.settings["phones"]:
            log.warning("номер не задан, SMS не отправлено: %s", text)
            return
        log.info("в очередь: %s", text)
        self.state["outbox"] = (self.state["outbox"] + [{"text": text, "sent_to": []}])[-OUTBOX_MAX:]
        save(self.state)

    def reading(self, source, ident, name, value, mono):
        remember(self.state["sensors"], source, ident, name, value, time.time())
        for th in self.thermos:
            if th.matches(ident, name):
                th.reading(value, mono)

    def step(self):
        mono, exact = time.monotonic(), SYNCED.exists()
        stamp = f", {when(time.time())}" if exact else ""
        self.reload(mono)
        s, state = self.settings, self.state

        if exact:
            note_power_on(state, self.boot, time.time(), uptime())
        if state["pending"] and (exact or mono - self.started > SYNC_WAIT):
            text, state["pending"] = power_message(state["pending"]), []
            if s["power"]["enabled"]:
                self.queue_sms(text)
            else:
                log.info("оповещение о питании выключено: %s", text)
                save(state)

        while not self.events.empty():
            kind, *rest = self.events.get_nowait()
            if kind == "temp":
                self.reading(*rest, mono)
            elif s["motion"]["enabled"] and self.motion.ready(mono, s["motion"]["pause"] * 60):
                self.queue_sms(f"Pi: движение у камеры{stamp}.")
        if mono >= self.next_bt:
            for addr, name, t in bluetooth_readings():
                self.reading("bluetooth", addr, name, t, mono)
            self.next_bt = mono + BT_EVERY
        for th in self.thermos:
            for text in th.check(mono, stamp):
                self.queue_sms(text)

        if mono >= self.next_beat:
            state.update(alive=time.time(), alive_exact=exact)
            save(state)
            self.next_beat = mono + BEAT_EVERY

        if state["outbox"] and mono >= self.next_try:
            try:
                send_next(self.modem, state, s["phones"])
                self.tries, state["error"] = 0, None
            except (OSError, ModemError) as e:
                log.warning("SMS пока не отправить: %s", e)
                self.tries += 1
                state["error"] = {"text": str(e), "at": time.time()}
            save(state)
            self.next_try = time.monotonic() + RETRY[min(self.tries, len(RETRY)) - 1] if self.tries else 0.0

    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stop", True))
        mqtt_listener(self.events)
        while not self.stop:
            self.step()
            time.sleep(1)
        self.state.update(alive=time.time(), alive_exact=SYNCED.exists(), clean=True)
        save(self.state)
        log.info("штатное выключение отмечено")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true", help="показать настройки, состояние и очередь SMS")
    ap.add_argument("--test", action="store_true", help="отправить проверочное SMS на номера из настроек")
    args = ap.parse_args()

    if args.status:
        print(json.dumps({"settings": sms_settings.load(), "state": load()}, ensure_ascii=False, indent=2))
        return
    if args.test:
        phones = sms_settings.load()["phones"]
        if not phones:
            sys.exit("номер не задан — впишите его в панели, раздел «SMS»")
        ok, failed = HiLink().send_sms(phones, "Pi: проверка SMS-оповещений.")
        print("ушло:", ", ".join(ok) or "—", "| не ушло:", ", ".join(failed) or "—")
        sys.exit(1 if failed else 0)
    Service().run()


if __name__ == "__main__":
    main()
