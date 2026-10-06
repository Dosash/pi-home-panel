"""BLE-датчики для панели: разбор рекламы термометров, гигрометров и т.п.

Точка входа — decode(). Чистые функции, только стандартная библиотека.
Форматы: BTHome v2, pvvx/ATC1441 (0x181A), Xiaomi MiBeacon (0xFE95),
Qingping (0xFDCD), SwitchBot Meter (0xFD3D/0x0D00 + 0x0969), Govee (manufacturer data).
"""

from __future__ import annotations

import struct


def _uuid(short):
    return "0000%04x-0000-1000-8000-00805f9b34fb" % short


UUID_BTHOME = _uuid(0xFCD2)
UUID_ATC = _uuid(0x181A)
UUID_MIBEACON = _uuid(0xFE95)
UUID_QINGPING = _uuid(0xFDCD)
UUID_SWITCHBOT = _uuid(0xFD3D)
UUID_SWITCHBOT_OLD = _uuid(0x0D00)

CID_APPLE = 0x004C
CID_SWITCHBOT = 0x0969
CID_GOVEE = 0xEC88  # не настоящий company id: Govee пишет после 0xFF байты 88 EC
CID_GOVEE_ALT = 0x0001  # H5101/H5102/H5108/H5177… (формально id Nokia)


def decode(name, service_data, manufacturer_data):
    """Разобрать одну рекламу. None — не наш датчик; исключений не бросает.

    Результат: {"brand", "model", "format", "encrypted", "values"}.
    """
    try:
        name = name if isinstance(name, str) else ""
        sd = {str(k).lower(): bytes(v) for k, v in (service_data or {}).items()}
        md = {int(k): bytes(v) for k, v in (manufacturer_data or {}).items()}
    except Exception:
        return None
    for parser in _PARSERS:
        try:
            result = parser(name, sd, md)
        except Exception:
            result = None
        if result is not None:
            return result
    return None


def _result(brand, model, fmt, values=None, encrypted=False):
    return {
        "brand": brand,
        "model": model,
        "format": fmt,
        "encrypted": encrypted,
        "values": {} if encrypted or values is None else values,
    }


def _scale(raw, factor):
    """Умножить на множитель и округлить до его разрядности (как bthome-ble)."""
    if factor == 1:
        return raw
    digits = -int(("%e" % factor).split("e")[1])
    return round(raw * factor, digits)


# --- BTHome v2 (https://bthome.io/format/) ---------------------------------

# Длины всех object id из спецификации — нужны, чтобы перешагивать
# объекты, которые мы не отображаем. 0x3B, 0x53, 0x54 — переменной длины.
_BTHOME_SIZE = {}
for _ids, _size in (
    ((0x00, 0x01, 0x09, 0x0F, 0x10, 0x11, 0x3A, 0x46, 0x57, 0x58, 0x59, 0x60, 0x64, 0x65), 1),
    (range(0x15, 0x30), 1),  # бинарные 0x15–0x2D, humidity/moisture uint8 0x2E/0x2F
    (
        (0x02, 0x03, 0x06, 0x07, 0x08, 0x0C, 0x0D, 0x0E, 0x12, 0x13, 0x14, 0x3C, 0x3D, 0x3F,
         0x40, 0x41, 0x43, 0x44, 0x45, 0x47, 0x48, 0x49, 0x4A, 0x51, 0x52, 0x56, 0x5A, 0x5D,
         0x5E, 0x5F, 0x61, 0xF0),
        2,
    ),
    ((0x04, 0x05, 0x0A, 0x0B, 0x42, 0x4B, 0xF2), 3),
    ((0x3E, 0x4C, 0x4D, 0x4E, 0x4F, 0x50, 0x55, 0x5B, 0x5C, 0x62, 0x63, 0xF1), 4),
):
    for _id in _ids:
        _BTHOME_SIZE[_id] = _size

# id -> (ключ в values, знаковое, множитель)
_BTHOME_MAP = {
    0x00: ("packet_id", False, 1),
    0x01: ("battery", False, 1),
    0x02: ("temperature", True, 0.01),
    0x03: ("humidity", False, 0.01),
    0x04: ("pressure", False, 0.01),
    0x05: ("illuminance", False, 0.01),
    0x08: ("dew_point", True, 0.01),
    0x0C: ("voltage", False, 0.001),
    0x0D: ("pm25", False, 1),
    0x0E: ("pm10", False, 1),
    0x11: ("opening", False, 1),
    0x12: ("co2", False, 1),
    0x14: ("moisture", False, 0.01),
    0x1A: ("opening", False, 1),  # door
    0x1B: ("opening", False, 1),  # garage door
    0x21: ("motion", False, 1),
    0x2D: ("opening", False, 1),  # window — так шлёт Shelly BLU Door/Window
    0x2E: ("humidity", False, 1),
    0x2F: ("moisture", False, 1),
    0x3A: ("button", False, 1),
    0x45: ("temperature", True, 0.1),
    0x4A: ("voltage", False, 0.1),
    0x56: ("conductivity", False, 1),
    0x57: ("temperature", True, 1),
    0x58: ("temperature", True, 0.35),
}

_BUTTON_EVENTS = {
    0x01: "press",
    0x02: "double_press",
    0x03: "triple_press",
    0x04: "long_press",
    0x05: "long_double_press",
    0x06: "long_triple_press",
    0x80: "hold_press",
}

# Точные имена Shelly и префиксы прошивок — как в bthome-ble.
_BTHOME_NAMES = {
    "SBHT-003C": "BLU H&T",
    "SBHT-203C": "BLU H&T ZB",
    "SBHT-103C": "BLU H&T Display ZB",
    "SBDW-002C": "BLU Door/Window",
    "SBMO-003Z": "BLU Motion",
    "SBBT-002C": "BLU Button1",
    "SBBT-102C": "BLU Button1 ZB",
    "SBBT-004CEU": "BLU Wall Switch 4",
    "SBBT-004CUS": "BLU RC Button 4",
    "SBTR-001AEU": "BLU TRV",
}


def _bthome_model(name):
    if name in _BTHOME_NAMES:
        return "Shelly", _BTHOME_NAMES[name]
    if name.startswith(("ATC", "LYWSD03MMC")):
        return "Xiaomi", "ATC"
    if name.startswith("prst"):
        return "b-parasite", "b-parasite"
    return "BTHome", "BTHome sensor"


def _bthome(name, sd, md):
    data = sd.get(UUID_BTHOME)
    if not data:
        return None
    info = data[0]  # бит 0 — шифрование, бит 2 — «по событию», биты 5–7 — версия
    if info >> 5 != 2:
        return None
    brand, model = _bthome_model(name)
    if info & 0x01:
        return _result(brand, model, "bthome-v2", encrypted=True)
    # Бит 1 в спецификации — резерв; bthome-ble считает его флагом «в начале 6 байт MAC».
    payload = data[7:] if info & 0x02 else data[1:]
    return _result(brand, model, "bthome-v2", _bthome_objects(payload))


def _bthome_objects(p):
    """Пройти список объектов. На неизвестном id или обрыве — вернуть что успели."""
    values, seen = {}, {}
    i, n = 0, len(p)
    while i < n:
        oid = p[i]
        if oid in (0x3B, 0x53, 0x54):
            if i + 1 >= n:
                break
            # 0x3B: младшие 5 бит — длина аргументов, плюс байт opcode
            size = (p[i + 1] & 0x1F) + 1 if oid == 0x3B else p[i + 1]
            start = i + 2
        else:
            size = _BTHOME_SIZE.get(oid)
            if size is None:
                break
            start = i + 1
        end = start + size
        if end > n:
            break
        spec = _BTHOME_MAP.get(oid)
        if spec:
            key, signed, factor = spec
            raw = int.from_bytes(p[start:end], "little", signed=signed)
            if key in ("opening", "motion"):
                val = bool(raw)
            elif key == "button":
                val = _BUTTON_EVENTS.get(raw)  # 0x00 — «нет события» для этой кнопки
            else:
                val = _scale(raw, factor)
            # Повторы одного типа: temperature, temperature_2, … (по порядку в пакете)
            seen[key] = seen.get(key, 0) + 1
            if val is not None:
                values[key if seen[key] == 1 else "%s_%d" % (key, seen[key])] = val
        i = end
    return values


# --- pvvx / ATC1441 (0x181A) -----------------------------------------------


def _atc(name, sd, md):
    d = sd.get(UUID_ATC)
    if d is None:
        return None
    n = len(d)
    if n == 15:
        # pvvx custom, всё LE: MAC[6] (перевёрнут), temp int16 ×0.01, hum uint16 ×0.01,
        # батарея мВ uint16, батарея %, счётчик, флаги
        t, h, mv, bat = struct.unpack_from("<hHHB", d, 6)
        values = {"temperature": round(t / 100, 2), "humidity": round(h / 100, 2), "voltage": mv / 1000, "battery": bat}
        return _result("Xiaomi", "ATC", "pvvx", values)
    if n == 13:
        # atc1441, всё BE: MAC[6], temp int16 ×0.1, hum %, батарея %, батарея мВ uint16, счётчик
        t, h, bat, mv = struct.unpack_from(">hBBH", d, 6)
        values = {"temperature": round(t / 10, 1), "humidity": h, "voltage": mv / 1000, "battery": bat}
        return _result("Xiaomi", "ATC", "atc1441", values)
    if n == 11:
        return _result("Xiaomi", "ATC", "pvvx", encrypted=True)
    if n == 8:
        return _result("Xiaomi", "ATC", "atc1441", encrypted=True)
    return None


# --- Xiaomi MiBeacon (0xFE95) ----------------------------------------------

# product id -> модель (подмножество devices.py из xiaomi-ble)
_XIAOMI_DEVICES = {
    0x0098: "HHCCJCY01",  # Flower Care
    0x015D: "HHCCPOT002",
    0x03BC: "GCLS002",
    0x01AA: "LYWSDCGQ",  # MJ_HT_V1
    0x045B: "LYWSD02",
    0x16E4: "LYWSD02MMC",
    0x2542: "LYWSD02MMC",
    0x055B: "LYWSD03MMC",
    0x0387: "MHO-C401",
    0x06D3: "MHO-C303",
    0x0347: "CGG1",
    0x0B48: "CGG1",  # в xiaomi-ble — «CGG1-ENCRYPTED»
    0x066F: "CGDK2",
    0x4F59: "CGDK3",
    0x0576: "CGD1",
    0x0C3C: "CGC1",
    0x1203: "XMWSDJ04MMC",
    0x2832: "MJWSD05MMC",
    0x4C47: "MJWSD05MMC",
    0x55B5: "MJWSD06MMC",
    0x5BEA: "MJWSD06MMC",
    0x5DB1: "MBS17",
    0x78DB: "ESM787",
    0x520B: "KS2BB",
    0x03D6: "CGH1",
    0x098B: "MCCGQ02HL",
    0x0A83: "CGPR1",
    0x0A8D: "RTCGQ02LM",
    0x07F6: "MJYD02YL",
}
_XIAOMI_BRANDS = {"ESM787": "Yanmi", "KS2BB": "Linptech"}


def _mibeacon(name, sd, md):
    d = sd.get(UUID_MIBEACON)
    if d is None or len(d) < 5:
        return None
    # frame control (LE): бит 3 шифрование, 4 MAC, 5 capability, 6 объекты, 7 mesh, 12–15 версия
    fc = d[0] | d[1] << 8
    version = fc >> 12
    if fc & 0x80 or version < 2:
        return None
    model = _XIAOMI_DEVICES.get(d[2] | d[3] << 8)
    if model is None:
        return None
    brand = _XIAOMI_BRANDS.get(model, "Xiaomi")
    fmt = "mibeacon-v%d" % version
    i = 5  # после product id и frame counter
    if fc & 0x10:
        i += 6
    if fc & 0x20:
        i += 1
        if len(d) >= i and d[i - 1] & 0x20:  # в capability есть IO — ещё байт
            i += 1
    if len(d) < i:
        return None
    if fc & 0x08:
        return _result(brand, model, fmt, encrypted=True)
    if not fc & 0x40:
        return _result(brand, model, fmt)
    return _result(brand, model, fmt, _mibeacon_objects(d[i:], model))


def _mibeacon_objects(p, model):
    """Объекты: id uint16 LE, длина uint8, данные."""
    v = {}
    i = 0
    while i + 3 <= len(p):
        oid, size = p[i] | p[i + 1] << 8, p[i + 2]
        x = p[i + 3 : i + 3 + size]
        if len(x) < size:
            break
        _mi_object(oid, x, model, v)
        i += 3 + size
    return v


def _mi_object(oid, x, model, v):
    n = len(x)
    if oid == 0x1004 and n == 2:
        v["temperature"] = round(struct.unpack("<h", x)[0] / 10, 1)
    elif oid == 0x1006 and n == 2:
        h = struct.unpack("<H", x)[0]
        # у этих моделей влажность «ступеньками» — xiaomi-ble отбрасывает дробь
        v["humidity"] = h // 10 if model in ("LYWSD03MMC", "MHO-C401") else round(h / 10, 1)
    elif oid == 0x100D and n == 4:
        t, h = struct.unpack("<hH", x)
        v["temperature"], v["humidity"] = round(t / 10, 1), round(h / 10, 1)
    elif oid in (0x100A, 0x4803, 0x4C03) and n >= 1:
        v["battery"] = x[0]
    elif oid == 0x1007 and n == 3 and model in ("HHCCJCY01", "GCLS002"):
        v["illuminance"] = int.from_bytes(x, "little")  # у ночников это флаг света, не люксы
    elif oid == 0x1008 and n >= 1:
        v["moisture"] = x[0]
    elif oid == 0x1009 and n == 2:
        v["conductivity"] = struct.unpack("<H", x)[0]
    elif oid == 0x1019 and n >= 1 and x[0] in (0, 1, 2):
        v["opening"] = x[0] != 1  # 0 открыто, 1 закрыто, 2 открыто слишком долго
    elif oid == 0x000F and n == 3:
        v["motion"] = True
        if model == "CGPR1":
            v["illuminance"] = int.from_bytes(x, "little")
    elif oid == 0x1017 and n == 4:
        v["motion"] = struct.unpack("<I", x)[0] <= 30  # секунд без движения
    elif oid in (0x4801, 0x4C01) and n == 4:
        v["temperature"] = round(struct.unpack("<f", x)[0], 1 if oid == 0x4801 else 2)
    elif oid in (0x4802, 0x4C02) and n == 1:
        v["humidity"] = x[0]
    elif oid == 0x4C08 and n == 4:
        v["humidity"] = round(struct.unpack("<f", x)[0], 1)
    elif oid == 0x4805 and n == 4:
        v["illuminance"] = round(struct.unpack("<f", x)[0], 1)


# --- Qingping (0xFDCD) -----------------------------------------------------

_QINGPING_DEVICES = {
    0x01: "CGG1",
    0x04: "CGH1",
    0x07: "CGG1",
    0x09: "CGP1W",
    0x0C: "CGD1",
    0x0E: "CGDN1",
    0x0F: "CGM1",
    0x10: "CGDK2",
    0x12: "CGPR1",
    0x15: "CGF1W",
    0x16: "CGG1",
    0x18: "CGP23W",
    0x1E: "CGC1",
    0x24: "CGDN1",
    0x26: "CGP23W",
    0x33: "CGP22C",
    0x4F: "CGG3",
    0x5D: "CGP22C",
}


def _qingping(name, sd, md):
    d = sd.get(UUID_QINGPING)
    if d is None or len(d) < 2:
        return None
    model = _QINGPING_DEVICES.get(d[1])
    if model is None:
        return None
    # [0] флаги (бит 6 — пакет-событие), [1] тип устройства, [2:8] MAC, дальше TLV: id, длина, данные
    event = d[0] & 0x40
    v = {}
    i = 8
    while i + 2 < len(d):
        tid, size = d[i], d[i + 1]
        x = d[i + 2 : i + 2 + size]
        if len(x) == size:
            if tid == 0x01 and size == 4:
                t, h = struct.unpack("<hH", x)
                v["temperature"], v["humidity"] = round(t / 10, 1), round(h / 10, 1)
            elif tid == 0x02 and size == 1:
                v["battery"] = x[0]
            elif tid == 0x04 and size == 1:
                v["opening"] = x[0] in (0, 2)  # 1 — закрыто, 2 — открыто слишком долго
            elif tid == 0x07 and size == 2:
                v["pressure"] = round(struct.unpack("<H", x)[0] / 10, 1)
            elif tid == 0x08 and size == 4:
                motion, lo, hi = struct.unpack("<BHB", x)
                v["motion"] = bool(motion)
                if not event:  # в пакете-событии хвост — не освещённость
                    v["illuminance"] = lo + (hi << 16)
            elif tid == 0x09 and size == 4:
                v["illuminance"] = struct.unpack("<I", x)[0]
            elif tid == 0x12 and size == 4:
                v["pm25"], v["pm10"] = struct.unpack("<HH", x)
            elif tid == 0x13 and size == 2:
                v["co2"] = struct.unpack("<H", x)[0]
        i += 2 + size
    return _result("Qingping", model, "qingping", v)


# --- SwitchBot Meter (0xFD3D / 0x0D00 + company 0x0969) --------------------

# Модель — по младшим 7 битам первого байта service data (как в pySwitchbot).
_SWITCHBOT_MODELS = {
    ord("T"): "Meter",
    ord("t"): "Meter",
    ord("i"): "Meter Plus",
    ord("I"): "Meter Plus",
    ord("w"): "Indoor/Outdoor Meter",
    ord("W"): "Indoor/Outdoor Meter",
    ord("4"): "Meter Pro",
    0x14: "Meter Pro",
    ord("5"): "Meter Pro CO2",
    0x15: "Meter Pro CO2",
}


def _switchbot(name, sd, md):
    s = sd.get(UUID_SWITCHBOT)
    if s is None:
        s = sd.get(UUID_SWITCHBOT_OLD)
    if not s:
        return None  # без service data модель не определить (pySwitchbot берёт её из кэша по MAC)
    model = _SWITCHBOT_MODELS.get(s[0] & 0x7F)
    if model is None:
        return None
    m = md.get(CID_SWITCHBOT)
    # 3 байта T/H: в manufacturer data [8:11] (после MAC и пары служебных байт), иначе service data [3:6]
    th = m[8:11] if m is not None and len(m) >= 11 else (s[3:6] if len(s) >= 6 else None)
    v = {}
    if th:
        # [0] биты 0–3 — десятые °C; [1] бит 7 — знак (1 = плюс), 0–6 — целые °C; [2] 0–6 — влажность
        t = (th[1] & 0x7F) + (th[0] & 0x0F) / 10
        if not th[1] & 0x80:
            t = -t
        h = th[2] & 0x7F
        if t or h:  # нули — признак пустого пакета
            v = {"temperature": round(t, 1) + 0.0, "humidity": h}  # + 0.0 убирает «-0.0»
            if len(s) >= 3:
                v["battery"] = s[2] & 0x7F
            if model == "Meter Pro CO2" and m is not None and len(m) >= 15:
                co2 = m[13] << 8 | m[14]  # BE
                if co2 <= 9999:  # выше паспортного диапазона — артефакт
                    v["co2"] = co2
    # Бит 7 первого байта pySwitchbot зовёт isEncrypted, но данные метров разбирает всё равно.
    return _result("SwitchBot", model, "switchbot", v)


# --- Govee (manufacturer data) ---------------------------------------------

_GOVEE_H51 = ("H5100", "H5101", "H5102", "H5103", "H5104", "H5105", "H5174", "H5177", "H5110", "GV5179")


def _govee_packed(b):
    """3 байта BE: знак в старшем бите, остальное = t×10000 + rh×10; 4-й — батарея, бит 7 — ошибка."""
    raw = b[0] << 16 | b[1] << 8 | b[2]
    num = raw & 0x7FFFFF
    t = (num // 1000) / 10
    if raw & 0x800000:
        t = -t
    v = {}
    if -40 <= t <= 100 and not b[3] & 0x80:
        v["temperature"], v["humidity"] = t, (num % 1000) / 10
    v["battery"] = b[3] & 0x7F
    return v


def _govee_le(d):
    """int16 LE ×0.01 °C, uint16 LE ×0.01 %, батарея % — с байта 1."""
    t, h, bat = struct.unpack_from("<hHB", d, 1)
    return {"temperature": round(t / 100, 2), "humidity": round(h / 100, 2), "battery": bat}


def _govee(name, sd, md):
    for cid, d in md.items():
        if cid == CID_APPLE:
            continue
        if len(d) > 25 and b"INTELLI_ROCKS" in d:
            d = d[:-25]  # иногда к пакету приклеен 25-байтный iBeacon-хвост
        n = len(d)
        # Порядок и условия — как в govee-ble: модель по длине, company id и имени.
        if n == 6 and ("H5072" in name or "H5075" in name or "H5129" in name or cid == CID_GOVEE):
            model = next((m for m in ("H5072", "H5075", "H5129") if m in name), "H5072/H5075")
            return _result("Govee", model, "govee", _govee_packed(d[1:5]))
        if n == 8 and ("5112" in name or "5140" in name):
            continue  # щупы H5112 и CO2-монитор H5140 — другой формат
        if n in (6, 8) and ("H5108" in name or any(m in name for m in _GOVEE_H51) or (cid == CID_GOVEE_ALT and n == 8)):
            if n == 8 or "H5108" in name:
                model = "H5108"
            else:
                model = next(m for m in _GOVEE_H51 if m in name)
            return _result("Govee", model, "govee", _govee_packed(d[2:6]))
        if n == 7 and ("H5074" in name or cid == CID_GOVEE):
            return _result("Govee", "H5074", "govee", _govee_le(d))
        if n == 9 and (cid == CID_GOVEE or any(m in name for m in ("H5051", "H5052", "H5071"))):
            model = next((m for m in ("H5071", "H5052") if m in name), "H5051")
            return _result("Govee", model, "govee", _govee_le(d))
    return None


_PARSERS = (_bthome, _atc, _mibeacon, _qingping, _switchbot, _govee)
