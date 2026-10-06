"""Датчики камеры в Home Assistant через MQTT: связь с камерой, движение, запись, готовые ролики.

HA находит их сам (MQTT discovery), они появляются как устройство «Камера».
"""
import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("webcam")

TOPIC = "webcam"  # webcam/status, webcam/motion, webcam/recording, webcam/online, webcam/clip
DISCOVERY = "homeassistant"

SENSORS = {
    "online": {"name": "Связь", "device_class": "connectivity", "entity_category": "diagnostic"},
    "motion": {"name": "Движение", "device_class": "motion"},
    "recording": {"name": "Запись", "icon": "mdi:record-rec"},
}


class HAMqtt(threading.Thread):
    def __init__(self, hub, motion, recorder, host, port):
        super().__init__(daemon=True, name="mqtt")
        self.hub, self.motion, self.recorder = hub, motion, recorder
        self.host, self.port = host, port
        self.sent = {}  # последнее отправленное состояние каждого датчика
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="webcam-panel")
        # Если панель упадёт, брокер сам объявит датчики недоступными.
        self.client.will_set(f"{TOPIC}/status", "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("MQTT: не подключился к %s:%d: %s", self.host, self.port, reason_code)
            return
        log.info("MQTT: подключён к %s:%d", self.host, self.port)
        self.announce()
        client.subscribe(f"{DISCOVERY}/status")  # HA перезапустился — объявимся заново
        self.sent = {}

    def on_message(self, client, userdata, msg):
        if msg.topic == f"{DISCOVERY}/status" and msg.payload == b"online":
            self.announce()
            self.sent = {}

    def announce(self):
        cam = self.hub.status.get("camera") or {}
        device = {
            "identifiers": ["webcam_pi"],
            "name": "Камера",
            "manufacturer": "Raspberry Pi",
            "model": cam.get("name") or "USB-камера",
        }
        for key, extra in SENSORS.items():
            config = {
                "unique_id": f"webcam_{key}",
                "state_topic": f"{TOPIC}/{key}",
                "availability_topic": f"{TOPIC}/status",
                "device": device,
                **extra,
            }
            self.client.publish(f"{DISCOVERY}/binary_sensor/webcam/{key}/config",
                                json.dumps(config, ensure_ascii=False), retain=True)
        self.client.publish(f"{TOPIC}/status", "online", retain=True)

    def clip_saved(self, path, name):
        """Готовый ролик: на webcam/clip можно повесить автоматизацию в HA (триггер MQTT)."""
        self.client.publish(f"{TOPIC}/clip", json.dumps({"name": name, "path": str(path)}), qos=1)

    def run(self):
        self.client.connect_async(self.host, self.port)
        self.client.loop_start()
        while True:
            values = {
                "online": self.hub.status.get("state") == "streaming",
                "motion": self.motion.moving(),
                "recording": self.recorder.current is not None,
            }
            if self.client.is_connected():
                for key, value in values.items():
                    if self.sent.get(key) != value:
                        self.client.publish(f"{TOPIC}/{key}", "ON" if value else "OFF", retain=True)
                        self.sent[key] = value
            time.sleep(0.5)
