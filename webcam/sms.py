"""SMS-оповещения в панели: настройки и состояние сервиса sms-alerts (~/project/alerts).

Шлёт оповещения сервис; панель только меняет его настройки (settings.json — он перечитывает сам),
показывает его состояние (state.json) и отправляет проверочное SMS.
"""
import json
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerts"))
import sms_settings  # noqa: E402

log = logging.getLogger("webcam")

STATE = Path("/var/lib/sms-alerts/state.json")
ALIVE = 150  # сек — сервис отмечается раз в минуту
TEST_TEXT = "Pi: проверка SMS-оповещений."


class SmsAlerts:
    def __init__(self, modem):
        self.modem = modem
        self.lock = threading.Lock()
        self.job = None  # проверочное SMS: {"state": sending|done|failed, ...}

    def status(self):
        try:
            state = json.loads(STATE.read_text())
        except (OSError, ValueError):
            state = {}
        modem = self.modem.status()
        sensors = [{"id": k, **v} for k, v in (state.get("sensors") or {}).items()]
        return {
            "settings": sms_settings.load(),
            "running": not state.get("clean", True) and time.time() - (state.get("alive") or 0) < ALIVE,
            "sim": modem.get("sim") if modem.get("present") else None,
            "outbox": [item.get("text") for item in state.get("outbox", [])],
            "log": list(reversed(state.get("log", []))),
            "error": state.get("error"),
            "sensors": sorted(sensors, key=lambda s: -(s.get("at") or 0)),
            "job": self.job,
        }

    def command(self, action, data):
        if action == "save":
            sms_settings.save(sms_settings.validate(data.get("settings")))
            log.info("SMS-оповещения: настройки сохранены")
        elif action == "test":
            # номера из формы — проверить можно и до сохранения
            phones = (sms_settings.phones(data["phones"]) if data.get("phones") is not None
                      else sms_settings.load()["phones"])
            if not phones:
                raise ValueError("впишите номер телефона")
            with self.lock:
                if self.job and self.job["state"] == "sending":
                    raise ValueError("проверочное SMS уже отправляется")
                self.job = {"state": "sending", "phones": phones, "time": time.time()}
            threading.Thread(target=self._test, args=(phones,), daemon=True, name="sms-test").start()
        else:
            raise ValueError(f"неизвестное действие: {action}")

    def _test(self, phones):
        try:
            ok, failed = self.modem.send_sms(phones, TEST_TEXT)
            job = {"state": "failed" if failed else "done", "ok": ok,
                   "error": "не ушло на " + ", ".join(failed) if failed else None}
        except Exception as e:  # ModemError, нет связи с модемом — показать в панели
            job = {"state": "failed", "error": str(e)}
        log.info("SMS-оповещения: проверочное SMS — %s", job.get("error") or "ушло")
        self.job = {**job, "phones": phones, "time": time.time()}
