"""Настройки SMS-оповещений: меняет панель (раздел «SMS»), читает сервис sms-alerts.

settings.json лежит рядом и в репозиторий не попадает — в нём номера телефонов.
"""
import copy
import json
import math
import os
import re
from pathlib import Path

PATH = Path(os.environ.get("SMS_SETTINGS") or Path(__file__).resolve().parent / "settings.json")
DEFAULTS = {
    "phones": [],
    "power": {"enabled": True},
    # stale — минут без показаний, после которых «датчик молчит»; 0 — не сообщать
    "temperature": {"enabled": True, "min": 0.0, "stale": 120, "sensors": []},
    "motion": {"enabled": False, "pause": 30},  # pause — минут между SMS о движении
}
MAX_PHONES, MAX_SENSORS = 5, 10
PHONE = re.compile(r"^\+?\d{10,15}$")


def load(path=None):
    """Текущие настройки; файла нет — умолчания."""
    try:
        data = json.loads((path or PATH).read_text())
    except (OSError, ValueError):
        data = {}
    try:
        return validate(data)
    except ValueError:  # файл пишет только панель и только проверенным — сюда не попадаем
        return copy.deepcopy(DEFAULTS)


def save(settings, path=None):
    path = path or PATH
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def phone(raw):
    p = re.sub(r"[\s()\-]", "", str(raw))
    if not PHONE.match(p):
        raise ValueError(f"«{str(raw).strip()}» не похоже на номер телефона")
    return p


def phones(raw):
    """«+7 999 000-00-01, 8 999 000-00-02» или список → ['+79990000001', '89990000002']."""
    items = re.split(r"[,;]", raw) if isinstance(raw, str) else list(raw or [])
    out = list(dict.fromkeys(phone(p) for p in items if str(p).strip()))
    if len(out) > MAX_PHONES:
        raise ValueError(f"не больше {MAX_PHONES} номеров")
    return out


def number(value, low, high, what):
    try:
        x = float(str(value).replace(",", ".").replace("−", "-"))
    except ValueError:
        raise ValueError(f"{what}: нужно число") from None
    if math.isnan(x) or not low <= x <= high:
        raise ValueError(f"{what}: от {low:g} до {high:g}")
    return x


def validate(data):
    """Присланное панелью → полные настройки (недостающее — из умолчаний). ValueError — понятным текстом."""
    if not isinstance(data, dict):
        raise ValueError("ожидается объект JSON")
    out = copy.deepcopy(DEFAULTS)
    out["phones"] = phones(data.get("phones"))
    for key in ("power", "temperature", "motion"):
        part = data.get(key) or {}
        if not isinstance(part, dict):
            raise ValueError(f"{key}: ожидается объект")
        if "enabled" in part:
            out[key]["enabled"] = bool(part["enabled"])

    t, m = data.get("temperature") or {}, data.get("motion") or {}
    if "min" in t:
        out["temperature"]["min"] = number(t["min"], -40, 40, "Порог температуры")
    if "stale" in t:
        out["temperature"]["stale"] = int(number(t["stale"], 0, 2880, "Сколько ждать молчащий датчик"))
    if "sensors" in t:
        sensors = {}
        for s in t["sensors"] or []:
            ident = str((s or {}).get("id") or "").strip()[:64]
            if ident:
                sensors[ident.lower()] = {"id": ident, "label": str(s.get("label") or "").strip()[:32] or ident}
        if len(sensors) > MAX_SENSORS:
            raise ValueError(f"не больше {MAX_SENSORS} датчиков")
        out["temperature"]["sensors"] = list(sensors.values())
    if "pause" in m:
        out["motion"]["pause"] = int(number(m["pause"], 1, 1440, "Пауза между SMS о движении"))
    return out
