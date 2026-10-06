"""Bluetooth Pi для панели: устройства рядом, сопряжение и подключение, показания BLE-датчиков.

Всё через BlueZ по D-Bus (Gio из PyGObject — он у панели уже есть). Тем же адаптером пользуется
Home Assistant: BlueZ делит поиск между клиентами, наш включается поверх поиска HA и выключается,
не трогая его. Агент сопряжения (коды для клавиатур и телефонов) живёт в своём потоке с циклом GLib:
только так D-Bus может звать нас обратно. Подтверждает он лишь сопряжение, начатое из панели.
"""
import logging
import re
import threading
import time

from gi.repository import Gio, GLib

try:
    from ble_sensors import decode as decode_sensor
except ImportError:  # без декодеров датчики просто не расшифровываются
    decode_sensor = None

log = logging.getLogger("webcam")

BLUEZ = "org.bluez"
ADAPTER = "/org/bluez/hci0"
ADAPTER_IF, DEVICE_IF, BATTERY_IF = "org.bluez.Adapter1", "org.bluez.Device1", "org.bluez.Battery1"
AGENT_PATH = "/home/panel/agent"
SCAN_SECONDS = 30
CACHE_SECONDS = 1
MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

# Производители по коду из рекламы — чтобы безымянное устройство было хоть как-то узнаваемо.
VENDORS = {0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google", 0x0157: "Huami",
           0x038F: "Xiaomi", 0x027D: "Huawei", 0x0171: "Amazon", 0x0087: "Garmin", 0x0310: "SGL Italia",
           0x02E5: "Espressif", 0x0059: "Nordic", 0x0969: "SwitchBot", 0xEC88: "Govee"}

AGENT_XML = """<node><interface name="org.bluez.Agent1">
  <method name="Release"/>
  <method name="Cancel"/>
  <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
  <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
  <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
  <method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
  <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
  <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
  <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
</interface></node>"""

# Ошибки BlueZ — человеческим языком. Сначала по тексту (он точнее), потом по имени ошибки.
MESSAGES = {
    "br-connection-profile-unavailable": "у Pi нет подходящего профиля для этого устройства",
    "br-connection-page-timeout": "устройство не отвечает — оно включено и рядом?",
    "Page Timeout": "устройство не отвечает — оно включено и рядом?",
    "Host is down": "устройство не отвечает — оно включено и рядом?",
    "le-connection-abort-by-local": "соединение не установилось — попробуйте ещё раз",
    "Software caused connection abort": "соединение не установилось — попробуйте ещё раз",
    "Authentication Failed": "устройство отклонило сопряжение",
}
ERRORS = {
    "org.bluez.Error.AuthenticationFailed": "устройство отклонило сопряжение",
    "org.bluez.Error.AuthenticationRejected": "устройство отклонило сопряжение",
    "org.bluez.Error.AuthenticationCanceled": "сопряжение отменено",
    "org.bluez.Error.AuthenticationTimeout": "устройство не ответило вовремя",
    "org.bluez.Error.ConnectionAttemptFailed": "устройство не отвечает — включите на нём режим сопряжения",
    "org.bluez.Error.InProgress": "уже выполняется — подождите",
    "org.bluez.Error.AlreadyConnected": "уже подключено",
    "org.bluez.Error.DoesNotExist": "устройство пропало из эфира — запустите поиск",
    "org.bluez.Error.NotReady": "Bluetooth выключен",
    "org.freedesktop.DBus.Error.NoReply": "устройство не ответило вовремя",
    "org.freedesktop.DBus.Error.ServiceUnknown": "служба Bluetooth не запущена",
}


def explain(e):
    remote = Gio.DBusError.get_remote_error(e) or ""
    text = e.message.split(": ", 1)[1] if e.message.startswith("GDBus.Error:") else e.message
    return MESSAGES.get(text) or ERRORS.get(remote) or text


class Bluetooth:
    def __init__(self):
        self.lock = threading.Lock()  # опрос состояния
        self.job_lock = threading.Lock()  # одна долгая команда за раз
        self.cache = (0.0, None)
        self.scan_until = 0.0
        self.job = None  # {"action", "address", "name", "state": running|done|failed, "error", "prompt", "time"}
        self.pairing = None  # путь устройства, которое сопрягаем из панели: агент отвечает только ему
        self.agent_ready = False
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            log.warning("Bluetooth: нет системной шины D-Bus: %s", e.message)
            self.bus = None
            return
        threading.Thread(target=self._agent_loop, daemon=True, name="bt-agent").start()

    # ---------- D-Bus ----------

    def _call(self, path, iface, method, args=None, timeout=10):
        return self.bus.call_sync(BLUEZ, path, iface, method, args, None, Gio.DBusCallFlags.NONE,
                                  timeout * 1000, None)

    def _set(self, path, iface, prop, value):
        self._call(path, "org.freedesktop.DBus.Properties", "Set", GLib.Variant("(ssv)", (iface, prop, value)))

    def _objects(self):
        return self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects").unpack()[0]

    # ---------- агент сопряжения ----------

    def _agent_loop(self):
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()  # вызовы агента придут в этот контекст, а крутим его мы
        node = Gio.DBusNodeInfo.new_for_xml(AGENT_XML)
        self.bus.register_object(AGENT_PATH, node.interfaces[0], self._agent_call, None, None)

        def appeared(conn, name, owner):  # и при старте, и после перезапуска bluetoothd
            try:
                self._call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
                           GLib.Variant("(os)", (AGENT_PATH, "KeyboardDisplay")))
                self.agent_ready = True
            except GLib.Error as e:
                log.warning("Bluetooth: агент сопряжения не зарегистрирован: %s", explain(e))

        def vanished(conn, name):
            self.agent_ready = False

        Gio.bus_watch_name_on_connection(self.bus, BLUEZ, Gio.BusNameWatcherFlags.NONE, appeared, vanished)
        GLib.MainLoop.new(ctx, False).run()

    def _prompt(self, kind, code):
        if self.job:
            self.job = {**self.job, "prompt": {"type": kind, "code": code}}

    def _agent_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method in ("Release", "Cancel"):
            if self.job:
                self.job = {**self.job, "prompt": None}
            invocation.return_value(None)
            return
        if not args or args[0] != self.pairing:
            # Сопряжение начали не из панели (телефон стучится к Pi и т.п.) — не подтверждаем.
            invocation.return_dbus_error("org.bluez.Error.Rejected", "pairing was not started from the panel")
            return
        if method == "RequestPinCode":  # старые устройства без экрана почти всегда ждут 0000
            self._prompt("pin", "0000")
            invocation.return_value(GLib.Variant("(s)", ("0000",)))
        elif method == "DisplayPinCode":
            self._prompt("pin", args[1])
            invocation.return_value(None)
        elif method == "DisplayPasskey":  # клавиатура: код набирают на ней самой
            self._prompt("type", f"{args[1]:06d}")
            invocation.return_value(None)
        elif method == "RequestConfirmation":  # телефон: код показан на обоих, подтверждают на нём
            self._prompt("confirm", f"{args[1]:06d}")
            invocation.return_value(None)
        elif method in ("RequestAuthorization", "AuthorizeService"):
            invocation.return_value(None)
        else:  # RequestPasskey: ввести код с экрана устройства — из панели пока нельзя
            invocation.return_dbus_error("org.bluez.Error.Rejected", "passkey entry is not supported")

    # ---------- команды ----------

    def command(self, action, address=None):
        """Команда из панели; ValueError — с текстом для страницы. Долгие идут в фоне, ход — в job."""
        if self.bus is None:
            raise ValueError("нет доступа к Bluetooth")
        if action == "scan":
            return self._scan()
        if action not in ("connect", "disconnect", "forget"):
            raise ValueError("неизвестная команда")
        address = (address or "").upper()
        if not MAC.match(address):
            raise ValueError("неверный адрес устройства")
        path = f"{ADAPTER}/dev_{address.replace(':', '_')}"
        try:
            dev = self._call(path, "org.freedesktop.DBus.Properties", "GetAll",
                             GLib.Variant("(s)", (DEVICE_IF,))).unpack()[0]
        except GLib.Error:
            raise ValueError("устройство пропало из эфира — запустите поиск") from None
        name = dev.get("Name") or address
        if action == "forget":
            try:  # убирает и сопряжение: подключить снова — только заново сопрягать
                self._call(ADAPTER, ADAPTER_IF, "RemoveDevice", GLib.Variant("(o)", (path,)))
            except GLib.Error as e:
                raise ValueError(explain(e)) from None
            log.info("Bluetooth: забыто %s (%s)", name, address)
            self.cache = (0.0, None)
            return
        if not self.job_lock.acquire(blocking=False):
            raise ValueError("уже выполняется другая команда — подождите")
        self.job = {"action": action, "address": address, "name": name, "state": "running",
                    "prompt": None, "time": time.time()}
        threading.Thread(target=self._run, args=(action, path, dev), daemon=True, name="bt-job").start()

    def _run(self, action, path, dev):
        try:
            if action == "disconnect":
                self._call(path, DEVICE_IF, "Disconnect", timeout=15)
            else:
                if not dev.get("Paired"):
                    self.pairing = path
                    self._call(path, DEVICE_IF, "Pair", timeout=60)
                if not dev.get("Trusted"):  # доверенное устройство потом подключается само, без вопросов
                    self._set(path, DEVICE_IF, "Trusted", GLib.Variant("b", True))
                self._call(path, DEVICE_IF, "Connect", timeout=30)
            log.info("Bluetooth: %s %s — готово", action, self.job["name"])
            self.job = {**self.job, "state": "done", "prompt": None, "time": time.time()}
        except GLib.Error as e:
            log.warning("Bluetooth: %s %s — %s", action, self.job["name"], e.message)
            self.job = {**self.job, "state": "failed", "error": explain(e), "prompt": None, "time": time.time()}
        finally:
            self.pairing = None
            self.cache = (0.0, None)
            self.job_lock.release()

    def _scan(self):
        with self.lock:
            if time.monotonic() < self.scan_until:
                return  # уже ищем
            try:  # и обычные устройства (наушники, колонки), и BLE; фильтр — только для нашего поиска
                self._call(ADAPTER, ADAPTER_IF, "SetDiscoveryFilter",
                           GLib.Variant("(a{sv})", ({"Transport": GLib.Variant("s", "auto")},)))
                self._call(ADAPTER, ADAPTER_IF, "StartDiscovery")
            except GLib.Error as e:
                if Gio.DBusError.get_remote_error(e) != "org.bluez.Error.InProgress":
                    raise ValueError(explain(e)) from None
            self.scan_until = time.monotonic() + SCAN_SECONDS
        timer = threading.Timer(SCAN_SECONDS, self._stop_scan)
        timer.daemon = True
        timer.start()
        log.info("Bluetooth: поиск устройств на %d с", SCAN_SECONDS)

    def _stop_scan(self):
        try:  # останавливает только наш поиск: поиск Home Assistant продолжается
            self._call(ADAPTER, ADAPTER_IF, "StopDiscovery")
        except GLib.Error:
            pass

    # ---------- состояние ----------

    def status(self):
        if self.bus is None:
            return {"adapter": None, "devices": [], "error": "нет доступа к D-Bus", "job": None}
        with self.lock:
            at, cached = self.cache
            if cached is None or time.monotonic() - at >= CACHE_SECONDS:
                try:
                    cached = self._read()
                except GLib.Error as e:
                    cached = {"adapter": None, "devices": [], "error": explain(e)}
                self.cache = (time.monotonic(), cached)
            left = max(0, round(self.scan_until - time.monotonic()))
        return {**cached, "scan_left": left, "agent": self.agent_ready, "job": self.job}

    def _read(self):
        objects = self._objects()
        adapter = objects.get(ADAPTER, {}).get(ADAPTER_IF)
        if adapter is None:
            return {"adapter": None, "devices": [], "error": "адаптер Bluetooth не найден"}
        devices = [self._device(ifaces) for ifaces in objects.values()
                   if ifaces.get(DEVICE_IF, {}).get("Adapter") == ADAPTER]
        return {
            "adapter": {"name": adapter.get("Alias") or adapter.get("Name"), "address": adapter.get("Address"),
                        "powered": bool(adapter.get("Powered")), "discovering": bool(adapter.get("Discovering"))},
            "devices": devices,
        }

    @staticmethod
    def _device(ifaces):
        d = ifaces[DEVICE_IF]
        name = d.get("Name")
        service_data = {k.lower(): bytes(v) for k, v in d.get("ServiceData", {}).items()}
        maker_data = {int(k): bytes(v) for k, v in d.get("ManufacturerData", {}).items()}
        sensor = None
        if decode_sensor and (service_data or maker_data):
            try:
                sensor = decode_sensor(name, service_data, maker_data)
            except Exception:  # чужой эфир бывает любым — панель из-за него падать не должна
                log.exception("Bluetooth: не разобрал рекламу %s", d.get("Address"))
            if sensor and not sensor.get("values") and not sensor.get("encrypted"):
                sensor = None  # маячок без показаний (часы, браслет) — это не датчик
        return {
            "address": d.get("Address"),
            "name": name,
            "icon": d.get("Icon"),
            "appearance": d.get("Appearance"),
            "le": d.get("AddressType") == "random" or d.get("Class") is None,
            "vendor": next((VENDORS[k] for k in maker_data if k in VENDORS), None),
            "rssi": d.get("RSSI"),
            "paired": bool(d.get("Paired")),
            "trusted": bool(d.get("Trusted")),
            "connected": bool(d.get("Connected")),
            "blocked": bool(d.get("Blocked")),
            "battery": ifaces.get(BATTERY_IF, {}).get("Percentage"),
            "sensor": sensor,
        }
