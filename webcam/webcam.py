#!/usr/bin/env python3
"""USB-вебкамера на веб-странице: эфир со звуком и запись роликов по движению.

Камера и её микрофон захватываются через GStreamer и работают постоянно;
последние секунды видео и звука держатся в памяти. Когда в кадре появляется
движение, пишется ролик (H.264 + AAC), начинающийся чуть раньше движения;
пока движение продолжается, следом пишутся новые ролики.
Если камеру выдернуть и вставить обратно, всё восстановится само.
"""
import argparse
import base64
import hmac
import io
import json
import logging
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import gi
import numpy as np
from PIL import Image

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402,F401  GstApp нужен для try_pull_sample

import panel  # noqa: E402
from ha_mqtt import TOPIC, HAMqtt  # noqa: E402
from modem import HiLink, ModemError, internet  # noqa: E402
from wifi import Hotspot  # noqa: E402
from bt import Bluetooth  # noqa: E402
from adguard import AdGuard  # noqa: E402
from sms import SmsAlerts  # noqa: E402
from zt import ZeroTier  # noqa: E402
from sysmon import SystemMonitor  # noqa: E402
from auth import COOKIE, Auth  # noqa: E402

Gst.init(None)
log = logging.getLogger("webcam")

HERE = Path(__file__).resolve().parent
PAGES = HERE / "pages"
RETRY_DELAY = 2  # сек между попытками найти камеру
WARMUP = 1.0  # сек после включения, пока камера подстраивает экспозицию
NO_FRAMES_TIMEOUT = 6  # сек без кадров, после которых камера считается потерянной
MOTION_RATE = 5  # проверок движения в секунду
MOTION_SETTLE = 3  # сек после включения камеры, когда движение не ищем (экспозиция)
MOTION_PIXEL_DIFF = 25  # на сколько (0-255) должна измениться яркость точки
MOTION_HOLD = 10  # сек: движение в конце ролика продлевает запись следующим роликом
QUEUE_LIMIT = 5000  # элементов в очереди записи (~30 с видео со звуком)
TRANSCODE_IDLE = 10  # сек без зрителей, после которых пережатие эфира выключается
AUDIO_RATE = 24000  # Гц, моно: для микрофона камеры хватает, в эфире ~48 КБ/с

# Качество эфира: имя → (ширина, высота, кадров/с, качество JPEG). None — как с камеры.
QUALITIES = {
    "1080p": None,
    "540p": (960, 540, 30, 75),
    "360p": (640, 360, 15, 70),
}

# Выставляются при каждом включении камеры; если у камеры такого нет, пропускаются.
CAMERA_CONTROLS = {
    "exposure_dynamic_framerate": 0,  # не снижать частоту кадров в темноте
    "power_line_frequency": 1,  # 50 Гц, иначе под лампами бегут полосы
}


# ---------- поиск камеры и микрофона ----------

def list_formats(device):
    """Возвращает {fourcc: [(w, h), ...]} для узла V4L2, пустой dict если захват не умеет."""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-formats-ext"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    formats, current = {}, None
    for line in out.splitlines():
        m = re.search(r"\[\d+\]: '(\w+)'", line)
        if m:
            current = formats.setdefault(m.group(1), [])
            continue
        m = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        if m and current is not None:
            current.append((int(m.group(1)), int(m.group(2))))
    return formats


def camera_name(device):
    path = Path(f"/sys/class/video4linux/{Path(device).name}/name")
    return path.read_text().split(":")[0].strip() if path.exists() else device


def find_usb_cameras():
    """Ищет в /sys USB-устройства V4L2, которые умеют отдавать видео."""
    cams = []
    nodes = Path("/sys/class/video4linux").glob("video*")
    for node in sorted(nodes, key=lambda p: int(p.name[5:])):
        if "/usb" not in os.path.realpath(node / "device"):
            continue
        device = f"/dev/{node.name}"
        # У UVC-камеры обычно два узла: видео и метаданные. У второго форматов нет.
        formats = list_formats(device)
        if formats:
            cams.append({"device": device, "name": camera_name(device), "formats": formats})
    return cams


def camera_by_device(device):
    formats = list_formats(device)
    if not formats:
        return None
    return {"device": device, "name": camera_name(device), "formats": formats}


def find_mic(video_device):
    """ALSA-устройство записи на том же USB-устройстве, что и камера (её микрофон)."""
    iface = os.path.realpath(f"/sys/class/video4linux/{Path(video_device).name}/device")
    usb_dev = os.path.dirname(iface)  # .../1-1.2/1-1.2:1.0 → .../1-1.2
    for card in sorted(Path("/sys/class/sound").glob("card*")):
        if not os.path.realpath(card / "device").startswith(usb_dev + "/"):
            continue
        num = card.name[4:]
        if Path(f"/proc/asound/card{num}/pcm0c").exists():
            return f"hw:{num},0"
    return None


def apply_controls(device):
    for name, value in CAMERA_CONTROLS.items():
        try:
            subprocess.run(
                ["v4l2-ctl", "-d", device, "-c", f"{name}={value}"],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
            )
        except subprocess.TimeoutExpired:
            # Бывает у UVC-камер сразу после освобождения; без настройки тоже работает.
            log.warning("Камера не ответила на %s=%s", name, value)


def pick_size(sizes, want):
    """Запрошенный размер, иначе самый большой не больше него, иначе самый маленький."""
    if not sizes:
        return None
    if want in sizes:
        return want
    fitting = [s for s in sizes if s[0] * s[1] <= want[0] * want[1]]
    if fitting:
        return max(fitting, key=lambda s: s[0] * s[1])
    return min(sizes, key=lambda s: s[0] * s[1])


def video_pipeline(cam, size, fps):
    """Описание конвейера GStreamer, который отдаёт в appsink готовые JPEG-кадры."""
    src = f"v4l2src device={cam['device']}"
    if "MJPG" in cam["formats"]:
        # Камера сама жмёт в JPEG: кадры просто пересылаются, CPU почти не нужен.
        size = pick_size(cam["formats"]["MJPG"], size)
        desc = f"{src} ! image/jpeg,width={size[0]},height={size[1]},framerate={fps}/1"
        mode = "MJPEG"
    else:
        # Сырой формат (YUYV и т.п.): жмём в JPEG на Pi.
        fourcc = next(iter(cam["formats"]))
        size = pick_size(cam["formats"][fourcc], size)
        desc = (f"{src} ! video/x-raw,width={size[0]},height={size[1]},framerate={fps}/1"
                " ! videoconvert ! jpegenc quality=85")
        mode = f"{fourcc} → JPEG"
    return desc + " ! appsink name=sink max-buffers=2 drop=true sync=false", size, mode


def h264_encoder(bitrate):
    """Аппаратный кодировщик Pi, если он есть, иначе программный x264."""
    if Gst.ElementFactory.find("v4l2h264enc"):
        return (f"v4l2convert ! video/x-raw,format=I420 ! v4l2h264enc "
                f"extra-controls=controls,video_bitrate={bitrate},h264_i_frame_period=60 "
                "! video/x-h264,level=(string)4")
    return (f"videoconvert ! x264enc speed-preset=ultrafast tune=zerolatency "
            f"bitrate={bitrate // 1000}")


def parse_bitrate(value):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", value)
    if not m:
        raise argparse.ArgumentTypeError("битрейт вида 8M или 4000k")
    mult = {"": 1, "k": 1000, "m": 1000_000}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def buffer_time(buf, pipeline):
    """Момент захвата буфера в секундах time.monotonic().

    Системные часы GStreamer идут по CLOCK_MONOTONIC, поэтому у видео и звука из разных
    конвейеров получается общая шкала времени — по ней они и синхронизируются в роликах.
    """
    if buf.pts == Gst.CLOCK_TIME_NONE:
        return time.monotonic()
    return (buf.pts + pipeline.get_base_time()) / Gst.SECOND


def launch(desc):
    """Конвейер на системных часах (CLOCK_MONOTONIC).

    Иначе alsasrc подсунет свои аудиочасы, и метки звука разойдутся с видео.
    """
    pipe = Gst.parse_launch(desc)
    pipe.use_clock(Gst.SystemClock.obtain())
    return pipe


def bus_error(pipeline):
    msg = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
    if msg is None:
        return None
    if msg.type == Gst.MessageType.EOS:
        return "поток закончился"
    err, debug = msg.parse_error()
    return f"{err.message} ({debug.splitlines()[0] if debug else ''})"


def wrap(data, pts=None, duration=None):
    buf = Gst.Buffer.new_wrapped(data)
    if pts is not None:
        buf.pts = int(pts * Gst.SECOND)
    if duration is not None:
        buf.duration = int(duration * Gst.SECOND)
    return buf


# ---------- общий кадр ----------

class Channel:
    """Последний JPEG-кадр одного качества эфира и ожидающие его зрители."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.times = deque(maxlen=90)

    def publish(self, frame):
        with self.cond:
            self.frame = frame
            self.seq += 1
            self.times.append(time.monotonic())
            self.cond.notify_all()

    def clear(self):
        with self.cond:
            self.frame = None
            self.times.clear()

    def wait_frame(self, after_seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq > after_seq, timeout)
            if self.seq > after_seq:
                return self.frame, self.seq
            return None, after_seq

    def fps(self):
        with self.cond:
            now = time.monotonic()
            recent = [t for t in self.times if now - t < 3]
        if len(recent) < 2:
            return 0.0
        return (len(recent) - 1) / (recent[-1] - recent[0])


class Hub:
    """Всё, что приходит с камеры: кадры для эфира, запас видео и звука, подписчики записи."""

    def __init__(self, preroll):
        self.lock = threading.Lock()
        self.video = Channel()
        self.preroll = preroll
        self.buffer = deque()  # ("v"|"a", время, данные) за последние preroll секунд
        self.subscribers = []  # очереди записи
        self.listeners = []  # очереди живого звука
        self.viewers = 0
        self.status = {"state": "starting", "camera": None}
        self.audio = None  # описание микрофона, если он есть
        self.started_at = time.monotonic()  # когда камера последний раз включилась

    def _push(self, item):
        with self.lock:
            self.buffer.append(item)
            while self.buffer and item[1] - self.buffer[0][1] > self.preroll:
                self.buffer.popleft()
            for q in self.subscribers:
                if q.qsize() < QUEUE_LIMIT:
                    q.put_nowait(item)
            return list(self.listeners) if item[0] == "a" else None

    def publish_video(self, frame, ts):
        self.status["state"] = "streaming"
        self._push(("v", ts, frame))
        self.video.publish(frame)

    def publish_audio(self, chunk, ts):
        for q in self._push(("a", ts, chunk)):
            if q.qsize() < 200:  # слушатель отстал на 4 с — лучше выкинуть, чем копить
                q.put_nowait(chunk)

    def subscribe(self, since):
        """Очередь новых данных плюс запас новее since, без пропусков между ними."""
        q = queue.Queue()
        with self.lock:
            backlog = [item for item in self.buffer if item[1] > since]
            self.subscribers.append(q)
        return q, backlog

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.remove(q)

    def listen(self):
        q = queue.Queue()
        with self.lock:
            self.listeners.append(q)
        return q

    def unlisten(self, q):
        with self.lock:
            self.listeners.remove(q)

    def video_fps(self):
        with self.lock:
            times = [ts for kind, ts, _ in self.buffer if kind == "v"]
        times = [t for t in times if times[-1] - t < 3] if times else []
        if len(times) < 2:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    def set_status(self, **kw):
        with self.lock:
            self.status = kw
            if kw.get("state") == "starting":
                self.started_at = time.monotonic()
            if kw.get("state") != "streaming":
                self.buffer.clear()
        if kw.get("state") != "streaming":
            self.video.clear()


# ---------- захват ----------

class AudioCapture(threading.Thread):
    """Микрофон камеры. Живёт своим конвейером, чтобы сбой звука не останавливал видео."""

    def __init__(self, hub, device):
        super().__init__(daemon=True, name="audio")
        self.hub, self.device = hub, device
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            pipe = launch(
                f"alsasrc device={self.device} buffer-time=200000 latency-time=20000 "
                # USB-микрофоны обычно умеют только стандартные частоты: берём 48 кГц
                # и понижаем сами, иначе ALSA отказывается открываться.
                "! audio/x-raw,rate=48000 ! audioconvert ! audioresample "
                f"! audio/x-raw,format=S16LE,rate={AUDIO_RATE},channels=1,layout=interleaved "
                "! appsink name=sink max-buffers=100 drop=true sync=false"
            )
            sink = pipe.get_by_name("sink")
            pipe.set_state(Gst.State.PLAYING)
            self.hub.audio = {"device": self.device, "rate": AUDIO_RATE}
            error = None
            try:
                while not self.stop.is_set():
                    sample = sink.try_pull_sample(Gst.SECOND)
                    if sample is None:
                        error = bus_error(pipe)
                        if error:
                            break
                        continue
                    buf = sample.get_buffer()
                    self.hub.publish_audio(buf.extract_dup(0, buf.get_size()),
                                           buffer_time(buf, pipe))
            finally:
                pipe.set_state(Gst.State.NULL)
                self.hub.audio = None
            if error:
                log.warning("Микрофон %s: %s", self.device, error)
                self.stop.wait(RETRY_DELAY)


class Capture(threading.Thread):
    def __init__(self, hub, args):
        super().__init__(daemon=True, name="capture")
        self.hub, self.args = hub, args

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("Ошибка захвата")
                self.hub.set_status(state="error", camera=None)
                time.sleep(RETRY_DELAY)

    def run_once(self):
        cam = camera_by_device(self.args.device) if self.args.device else None
        if not self.args.device:
            cams = find_usb_cameras()
            cam = cams[0] if cams else None
        if cam is None:
            self.hub.set_status(state="no_camera", camera=None)
            time.sleep(RETRY_DELAY)
            return

        desc, size, mode = video_pipeline(cam, self.args.size, self.args.fps)
        mic = None
        if not self.args.no_audio:
            mic = self.args.audio_device or find_mic(cam["device"])
        log.info("Камера %s (%s), %s %dx%d, микрофон %s",
                 cam["name"], cam["device"], mode, *size, mic or "нет")
        apply_controls(cam["device"])
        self.hub.set_status(
            state="starting",
            camera={
                "name": cam["name"],
                "device": cam["device"],
                "mode": mode,
                "size": f"{size[0]}x{size[1]}",
            },
        )

        pipe = launch(desc)
        sink = pipe.get_by_name("sink")
        audio = AudioCapture(self.hub, mic) if mic else None
        pipe.set_state(Gst.State.PLAYING)
        if audio:
            audio.start()
        warm_at = time.monotonic() + WARMUP
        last_frame = time.monotonic()
        error = None
        try:
            while True:
                sample = sink.try_pull_sample(Gst.SECOND)
                if sample is None:
                    error = bus_error(pipe)
                    if not error and time.monotonic() - last_frame > NO_FRAMES_TIMEOUT:
                        error = f"нет кадров {NO_FRAMES_TIMEOUT} с"
                    if error:
                        break
                    continue
                last_frame = time.monotonic()
                if last_frame < warm_at:
                    continue
                buf = sample.get_buffer()
                self.hub.publish_video(buf.extract_dup(0, buf.get_size()),
                                       buffer_time(buf, pipe))
        finally:
            if audio:
                audio.stop.set()
                audio.join(5)
            pipe.set_state(Gst.State.NULL)

        log.warning("Камера остановилась: %s", error)
        self.hub.set_status(state="error", camera=None, error=error)
        time.sleep(RETRY_DELAY)


# ---------- качество эфира ----------

class Transcoder:
    """Пережимает эфир в меньшее разрешение, пока его кто-то смотрит."""

    def __init__(self, hub, name, width, height, fps, quality):
        self.hub, self.name = hub, name
        self.size, self.fps, self.quality = (width, height), fps, quality
        self.channel = Channel()
        self.lock = threading.Lock()
        self.viewers = 0
        self.last_viewer = 0.0
        self.thread = None

    def acquire(self):
        with self.lock:
            self.viewers += 1
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, daemon=True,
                                               name=f"transcode-{self.name}")
                self.thread.start()

    def release(self):
        with self.lock:
            self.viewers -= 1
            self.last_viewer = time.monotonic()

    def wanted(self):
        with self.lock:
            return self.viewers > 0 or time.monotonic() - self.last_viewer < TRANSCODE_IDLE

    def run(self):
        w, h = self.size
        # Декодирует libjpeg-turbo, масштабирует ISP Pi (v4l2convert), жмёт снова libjpeg.
        scale = (f"v4l2convert ! video/x-raw,width={w},height={h},format=I420"
                 if Gst.ElementFactory.find("v4l2convert")
                 else f"videoscale ! video/x-raw,width={w},height={h}")
        pipe = Gst.parse_launch(
            "appsrc name=src format=time is-live=true caps=image/jpeg,framerate=0/1 "
            "! queue max-size-buffers=2 leaky=downstream ! jpegdec "
            f"! queue max-size-buffers=2 leaky=downstream ! {scale} "
            f"! jpegenc quality={self.quality} "
            "! appsink name=sink max-buffers=2 drop=true sync=false"
        )
        src, sink = pipe.get_by_name("src"), pipe.get_by_name("sink")
        pipe.set_state(Gst.State.PLAYING)
        log.info("Эфир %s включён", self.name)
        stop = threading.Event()

        def pull():
            while not stop.is_set():
                sample = sink.try_pull_sample(Gst.SECOND // 2)
                if sample is not None:
                    buf = sample.get_buffer()
                    self.channel.publish(buf.extract_dup(0, buf.get_size()))

        puller = threading.Thread(target=pull, daemon=True)
        puller.start()
        try:
            seq, next_at = 0, 0.0
            while self.wanted():
                frame, seq = self.hub.video.wait_frame(seq, timeout=1)
                now = time.monotonic()
                if frame is None or now < next_at:
                    continue  # лишние кадры пропускаем, чтобы держать нужную частоту
                next_at = max(next_at + 1 / self.fps, now - 1 / self.fps)
                src.push_buffer(wrap(frame, pts=now))
                error = bus_error(pipe)
                if error:
                    log.warning("Эфир %s: %s", self.name, error)
                    break
        finally:
            stop.set()
            puller.join(2)
            pipe.set_state(Gst.State.NULL)
            self.channel.clear()
            log.info("Эфир %s выключен", self.name)


# ---------- движение ----------

class MotionDetector(threading.Thread):
    """Сравнивает соседние уменьшенные кадры и отмечает, когда в кадре что-то двигалось."""

    def __init__(self, hub, area):
        super().__init__(daemon=True, name="motion")
        self.hub = hub
        self.area = area  # доля изменившихся точек (0-1), выше которой считаем движением
        self.level = 0.0  # последняя доля изменившихся точек
        self.last_motion = 0.0
        self.started = threading.Event()  # выставляется в начале движения

    @staticmethod
    def small_gray(jpeg):
        img = Image.open(io.BytesIO(jpeg))
        # draft просит libjpeg декодировать сразу в уменьшенном виде: в разы быстрее.
        img.draft("L", (img.width // 8, img.height // 8))
        img = img.convert("L").resize((160, 90))
        arr = np.asarray(img, dtype=np.int16)
        # Вычитаем среднюю яркость, чтобы подстройка экспозиции не считалась движением.
        return arr - int(arr.mean())

    def run(self):
        prev, seq, hits = None, 0, 0
        while True:
            time.sleep(1 / MOTION_RATE)
            frame, new_seq = self.hub.video.wait_frame(seq, timeout=5)
            if frame is None:
                prev, hits = None, 0
                continue
            seq = new_seq
            if time.monotonic() - self.hub.started_at < MOTION_SETTLE:
                prev, hits = None, 0
                continue
            try:
                cur = self.small_gray(frame)
            except Exception as e:
                log.debug("не разобрал кадр: %s", e)
                continue
            if prev is not None:
                self.level = float(np.mean(np.abs(cur - prev) > MOTION_PIXEL_DIFF))
                # Две проверки подряд, чтобы одиночная помеха не запускала запись.
                hits = hits + 1 if self.level > self.area else 0
                if hits >= 2:
                    if not self.moving():
                        log.info("Движение: %.1f%% кадра", self.level * 100)
                    self.last_motion = time.monotonic()
                    self.started.set()
            prev = cur

    def moving(self, within=2.0):
        return time.monotonic() - self.last_motion < within


# ---------- запись ----------

class Recorder(threading.Thread):
    def __init__(self, hub, motion, args):
        super().__init__(daemon=True, name="recorder")
        self.hub, self.motion, self.args = hub, motion, args
        self.dir = args.record_dir
        self.current = None  # имя файла, который пишется сейчас
        self.last_written = 0.0  # время последнего записанного кадра
        self.listeners = []  # вызываются с (путь, имя) для каждого готового ролика
        self.hooks = []  # потоки --on-clip, которые ещё могут работать
        self.stopping = threading.Event()  # панель останавливается: дописать ролик и выйти
        # Какие ролики — одно событие (пишутся подряд, пока движение не кончится):
        # {ролик: первый ролик события}. Скрытый файл — в медиа HA не виден.
        self.events_file = self.dir / ".events.json"
        try:
            self.events = json.loads(self.events_file.read_text())
        except (OSError, ValueError):
            self.events = {}
        self.event_id = None

    def run(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for part in self.dir.rglob("*.part"):
            part.unlink()  # недописанные ролики после сбоя
        for old in self.dir.rglob("[!.]*.jpg"):
            old.rename(old.with_name("." + old.name))  # миниатюры старого формата
        while not self.stopping.is_set():
            if not self.motion.started.wait(1):
                continue
            self.motion.started.clear()
            try:
                self.record_event()
            except Exception:
                log.exception("Ошибка записи")
                self.current = None
                time.sleep(RETRY_DELAY)

    def stop(self, timeout):
        """Дописать текущий ролик и остановиться (вызывается при остановке панели)."""
        self.stopping.set()
        self.join(timeout)

    def wait_hooks(self, timeout):
        """Ждёт --on-clip: после выхода панели systemd завершит их вместе с ней."""
        hooks = [t for t in self.hooks if t.is_alive()]
        if hooks:
            log.info("Остановка: жду --on-clip (%d)", len(hooks))
        deadline = time.monotonic() + timeout
        for hook in hooks:
            hook.join(max(deadline - time.monotonic(), 0))

    def record_event(self):
        """Пишет ролики подряд, пока движение не прекратится."""
        q, backlog = self.hub.subscribe(since=self.last_written)
        self.event_id = None
        try:
            while True:
                backlog = self.record_clip(q, backlog)
                if backlog is None or self.stopping.is_set():
                    break  # камера пропала или панель останавливается
                if not self.motion.moving(within=MOTION_HOLD):
                    break
                log.info("Движение продолжается, следующий ролик")
        finally:
            # «Идёт запись» гаснет, только когда последний ролик события уже лежит на месте
            # со своей миниатюрой; между роликами одного события не мигает.
            self.current = None
            self.hub.unsubscribe(q)
            self.motion.started.clear()

    def clip_pipeline(self, part, with_audio):
        desc = (
            "appsrc name=v format=time caps=image/jpeg,framerate=0/1 "
            f"! queue ! jpegdec ! queue ! {h264_encoder(self.args.bitrate)} "
            "! h264parse ! queue ! mp4mux name=mux faststart=true "
            f"! filesink location=\"{part}\" "
        )
        if with_audio:
            desc += (
                "appsrc name=a format=time "
                f"caps=audio/x-raw,format=S16LE,rate={AUDIO_RATE},channels=1,layout=interleaved "
                "! queue ! audioconvert ! avenc_aac bitrate=64000 ! aacparse ! queue ! mux. "
            )
        return Gst.parse_launch(desc)

    def record_clip(self, q, backlog):
        """Пишет один ролик. Возвращает данные, не попавшие в него, или None, если камеры нет."""
        name = datetime.now().strftime("%Y-%m-%d/%H-%M-%S.mp4")
        path = self.dir / name
        path.parent.mkdir(exist_ok=True)
        part = path.with_name(path.name + ".part")
        with_audio = self.hub.audio is not None or any(k == "a" for k, _, _ in backlog)
        pipe = self.clip_pipeline(part, with_audio)
        vsrc, asrc = pipe.get_by_name("v"), pipe.get_by_name("a")
        pipe.set_state(Gst.State.PLAYING)
        frame_len = 1 / (self.hub.video_fps() or self.args.fps)
        log.info("Запись %s (из запаса %.1f с)", name,
                 (backlog[-1][1] - backlog[0][1]) if backlog else 0)
        self.current = name

        pending = deque(backlog)
        leftover = []
        start = end = None
        thumb = last_frame = None
        alive = True
        try:
            # Запас из памяти дописываем и при остановке: движение как раз в его конце.
            while pending or not self.stopping.is_set():
                if pending:
                    kind, ts, data = pending.popleft()
                else:
                    try:
                        kind, ts, data = q.get(timeout=5)
                    except queue.Empty:
                        alive = False
                        break
                    if kind == "v" and thumb is None:
                        thumb = data  # кадр, на котором началось движение
                if kind == "a":
                    if start is None or asrc is None or ts < start:
                        continue  # звук до первого кадра не нужен
                    if ts >= end:
                        leftover.append((kind, ts, data))
                        continue
                    asrc.push_buffer(wrap(data, pts=ts - start))
                    continue
                if start is None:
                    start, end = ts, ts + self.args.clip
                if ts >= end:
                    leftover.append((kind, ts, data))
                    break
                vsrc.push_buffer(wrap(data, pts=ts - start, duration=frame_len))
                self.last_written, last_frame = ts, data
                error = bus_error(pipe)
                if error:
                    log.error("Запись %s: %s", name, error)
                    break
        finally:
            vsrc.end_of_stream()
            if asrc:
                asrc.end_of_stream()
            msg = pipe.get_bus().timed_pop_filtered(
                30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            pipe.set_state(Gst.State.NULL)

        ok = msg is not None and msg.type == Gst.MessageType.EOS and start is not None
        if not ok:
            reason = msg.parse_error()[0].message if msg and msg.type == Gst.MessageType.ERROR \
                else "ролик пустой" if start is None else "не дописался за 30 с"
            log.error("Ролик %s не сохранён: %s", name, reason)
            part.unlink(missing_ok=True)
        else:
            part.rename(path)
            log.info("Сохранён %s (%.1f МБ)", name, path.stat().st_size / 2**20)
            # Новых кадров не дождались (остановка, камера пропала) — последний из записанных.
            thumb = thumb or last_frame
            if thumb is not None:
                save_thumbnail(thumb, thumb_path(path))
            self.event_id = self.event_id or name
            self.remember_event(name, self.event_id)
            self.on_saved(path)
        return leftover if alive else None

    def remember_event(self, name, event_id):
        self.events[name] = event_id
        # удалённые по лимиту ролики из списка убираем
        self.events = {k: v for k, v in self.events.items() if (self.dir / k).exists()}
        tmp = self.events_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.events))
        tmp.replace(self.events_file)

    def on_saved(self, path):
        self.cleanup()
        for listener in self.listeners:
            listener(path, path.relative_to(self.dir).as_posix())
        if self.args.on_clip:
            cmd = shlex.split(self.args.on_clip) + [str(path)]
            hook = threading.Thread(target=run_hook, args=(cmd,), daemon=True)
            hook.start()
            self.hooks = [t for t in self.hooks if t.is_alive()] + [hook]

    def cleanup(self):
        """Удаляет самые старые ролики, пока папка не станет меньше лимита."""
        limit = self.args.max_storage_gb * 2**30
        clips = sorted(self.dir.rglob("*.mp4"))
        total = sum(p.stat().st_size for p in clips)
        for p in clips:
            if total <= limit:
                break
            total -= p.stat().st_size
            p.unlink()
            thumb_path(p).unlink(missing_ok=True)
            log.info("Удалён старый ролик %s", p.relative_to(self.dir))
            if p.parent != self.dir and not any(p.parent.iterdir()):
                p.parent.rmdir()


def thumb_path(clip):
    """Миниатюра ролика: скрытый файл рядом (.ЧЧ-ММ-СС.jpg) — HA не показывает скрытые в медиа."""
    return clip.with_name(f".{clip.stem}.jpg")


def save_thumbnail(jpeg, path):
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.draft("RGB", (img.width // 4, img.height // 4))
        img = img.convert("RGB")
        img.thumbnail((480, 270))
        img.save(path, quality=80)
    except Exception as e:
        log.warning("Не сохранил миниатюру %s: %s", path.name, e)


def run_hook(cmd):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error("--on-clip не выполнился: %s", e)
        return
    if res.returncode != 0:
        log.error("--on-clip вернул %d: %s", res.returncode, res.stderr.strip()[-500:])


# ---------- веб ----------

class Handler(BaseHTTPRequestHandler):
    hub: Hub
    motion: MotionDetector
    recorder: Recorder
    sysmon: SystemMonitor
    modem: HiLink
    zerotier: ZeroTier
    wifi: Hotspot
    bt: Bluetooth
    adguard: AdGuard
    sms: SmsAlerts
    adguard_port: int
    transcoders: dict
    auth: Auth
    ha_port: int

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def parse(self):
        url = urlsplit(self.path)
        self.route = unquote(url.path)
        self.query = {k: v[0] for k, v in parse_qs(url.query).items()}

    def require_login(self):
        """True, если можно продолжать. Иначе отправляет на вход (или 401 для API)."""
        if not panel.host_allowed(self):
            panel.forbid_host(self)
            return False
        if panel.logged_in(self, self.auth):
            return True
        page = self.route in ("/", "/camera") or self.route.startswith("/z2m")
        if self.command == "GET" and page:
            self.redirect("/login?next=" + quote(self.path, safe=""))
        else:
            self.send_body(b'{"error": "login required"}', "application/json",
                           HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self):
        self.parse()
        routes = {
            "/login": self.login_page,
            "/logout": self.logout,
            "/": lambda: self.send_page("home.html"),
            "/camera": lambda: self.send_page("camera.html"),
            "/api/overview": self.overview,
            "/api/system": self.system,
            "/api/internet": self.internet,
            "/api/wifi": self.wifi_status,
            "/wifi-qr.svg": self.wifi_qr,
            "/api/bluetooth": self.bt_status,
            "/api/adguard": self.adguard_status,
            "/api/sms": self.sms_status,
            "/stream.mjpg": self.stream,
            "/audio.pcm": self.audio,
            "/snapshot.jpg": self.snapshot,
            "/status": self.status,
            "/recordings.json": self.recordings,
        }
        try:
            if self.route != "/login" and not self.require_login():
                return
            if self.route in routes:
                routes[self.route]()
            elif self.route.startswith("/recordings/"):
                self.recording_file(self.route.removeprefix("/recordings/"))
            elif self.route.startswith("/z2m"):
                self.zigbee2mqtt()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        self.parse()
        try:
            if self.route == "/login":
                self.login()
            elif self.route.startswith("/api/modem/"):
                if self.require_login():
                    self.modem_action(self.route.removeprefix("/api/modem/"))
            elif self.route == "/api/wifi":
                if self.require_login():
                    self.wifi_settings()
            elif self.route == "/api/bluetooth":
                if self.require_login():
                    self.bt_command()
            elif self.route == "/api/adguard":
                if self.require_login():
                    self.adguard_command()
            elif self.route == "/api/sms":
                if self.require_login():
                    self.sms_command()
            elif not self.route.startswith("/z2m"):
                self.send_error(HTTPStatus.NOT_FOUND)
            elif self.require_login():
                self.zigbee2mqtt()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- вход ----------

    def redirect(self, location, cookie=None):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def after_login(self, nxt):
        """Куда вернуть после входа: только свои адреса, «ha» — Home Assistant."""
        if nxt == "ha":
            return panel.ha_url(self, self.ha_port)
        if nxt == "adguard":
            return panel.ha_url(self, self.adguard_port)
        if nxt.startswith("/") and not nxt.startswith("//"):
            return nxt
        return "/"

    def login_page(self):
        if panel.logged_in(self, self.auth):
            cookie = None
            if not self.auth.valid(panel.session_token(self)):
                # Вошли по HTTP Basic: выдаём и cookie, иначе порт Home Assistant не узнает нас.
                cookie = self.session_cookie(remember=False)
            self.redirect(self.after_login(self.query.get("next", "/")), cookie)
            return
        self.send_page("login.html")

    def session_cookie(self, remember):
        token, max_age = self.auth.create(remember)
        cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
        return cookie + f"; Max-Age={max_age}" if max_age else cookie

    def login(self):
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        nxt = form.get("next", "/")
        ip = self.client_address[0]
        ok, error = self.auth.login(ip, form.get("user", ""), form.get("password", ""))
        if not ok:
            log.warning("Неудачный вход с %s", ip)
            self.redirect(f"/login?error={quote(error)}&next={quote(nxt, safe='')}")
            return
        log.info("Вход с %s", ip)
        self.redirect(self.after_login(nxt), self.session_cookie(form.get("remember") == "on"))

    def logout(self):
        self.auth.drop(panel.session_token(self))
        self.redirect("/login", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    # ---------- панель ----------

    def send_page(self, name):
        self.send_body((PAGES / name).read_bytes(), "text/html; charset=utf-8")

    def overview(self):
        body = panel.overview()
        body["ha_url"] = panel.ha_url(self, self.ha_port)
        body["zapret_url"] = panel.ha_url(self, panel.ZAPRET_PANEL_PORT)
        body["camera"] = {
            **self.hub.status,
            "motion": self.motion.moving(),
            "recording": self.recorder.current,
        }
        body["auth"] = self.auth.enabled
        self.send_body(json.dumps(body, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def internet(self):
        body = internet(self.modem)
        body["zerotier"] = self.zerotier.status()
        body["ports"] = {"panel": self.server.server_port, "ha": self.ha_port}
        self.send_body(json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def modem_action(self, action):
        """Управление модемом. Только JSON: обычная HTML-форма с чужого сайта так не отправит."""
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_body(b'{"error": "json required"}', "application/json",
                           HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if action == "data":
                self.modem.set_data(bool(data.get("on")))
            elif action == "mode":
                self.modem.set_mode(str(data.get("mode")))
            elif action == "reconnect":
                self.modem.reconnect()
            elif action == "reboot":
                self.modem.reboot()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        except (ValueError, OSError, ModemError) as e:
            # ValueError — неверный запрос (не JSON, нет такого режима), остальное — модем не справился
            status = HTTPStatus.BAD_REQUEST if isinstance(e, ValueError) else HTTPStatus.BAD_GATEWAY
            log.warning("Модем, %s: %s", action, e)
            # Сетевые ошибки Python («timed out», «Connection refused») — не для страницы.
            text = str(e) if isinstance(e, (ValueError, ModemError)) else "модем не ответил"
            body = json.dumps({"error": text}, ensure_ascii=False).encode()
            self.send_body(body, "application/json; charset=utf-8", status)
            return
        log.info("Модем: %s %s", action, data or "")
        self.send_body(b'{"ok": true}', "application/json")

    def wifi_status(self):
        self.send_body(json.dumps(self.wifi.status(), ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def wifi_qr(self):
        svg = self.wifi.qr_svg()
        if svg is None:
            self.send_body("пароль Wi-Fi недоступен".encode(), "text/plain; charset=utf-8",
                           HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_body(svg, "image/svg+xml")

    def json_command(self, handle):
        """POST-команда с телом-объектом JSON: handle(data) делает дело, ValueError — ответ 400 с текстом.
        Только JSON: обычная HTML-форма с чужого сайта так не отправит."""
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_body(b'{"error": "json required"}', "application/json",
                           HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("ожидается объект JSON")
            handle(data)
        except ValueError as e:  # и битый JSON, и неверная команда
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode()
            self.send_body(body, "application/json; charset=utf-8", HTTPStatus.BAD_REQUEST)
            return
        # Долгое выполняется в фоне: ответ уходит сразу, ход дел — в поле job у состояния.
        self.send_body(b'{"ok": true}', "application/json", HTTPStatus.ACCEPTED)

    def wifi_settings(self):
        def apply(data):
            password = data.get("password") or None
            self.wifi.apply(str(data.get("ssid") or ""), None if password is None else str(password),
                            str(data.get("band") or ""), data.get("channel"))
        self.json_command(apply)

    def adguard_status(self):
        body = {**self.adguard.status(), "url": panel.ha_url(self, self.adguard_port)}
        self.send_body(json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def sms_status(self):
        self.send_body(json.dumps(self.sms.status(), ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def sms_command(self):
        self.json_command(lambda data: self.sms.command(str(data.get("action") or ""), data))

    def adguard_command(self):
        self.json_command(lambda data: self.adguard.command(str(data.get("action") or ""), data.get("minutes")))

    def bt_status(self):
        self.send_body(json.dumps(self.bt.status(), ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def bt_command(self):
        self.json_command(lambda data: self.bt.command(str(data.get("action") or ""),
                                                       str(data.get("address") or "")))

    def system(self):
        self.send_body(json.dumps(self.sysmon.snapshot(), ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def zigbee2mqtt(self):
        if self.route == "/z2m":
            self.redirect("/z2m/")
            return
        if not panel.proxy(self, panel.Z2M_PORT):
            text = ("Вставьте Zigbee-адаптер (Sonoff, ConBee, SLZB и т.п.) в USB-порт Pi — "
                    "Zigbee2MQTT найдёт его и запустится сам в течение минуты."
                    if not panel.zigbee_adapter() else
                    "Адаптер найден, Zigbee2MQTT запускается. "
                    "Если долго не открывается — смотрите journalctl -u zigbee2mqtt.")
            body = panel.unavailable_page("Zigbee2MQTT не запущен", text)
            self.send_body(body, "text/html; charset=utf-8", HTTPStatus.SERVICE_UNAVAILABLE)

    def send_body(self, body, content_type, status=HTTPStatus.OK, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def status(self):
        hub = self.hub
        body = {
            **hub.status,
            "fps": round(hub.video.fps(), 1),
            "viewers": hub.viewers,
            "audio": hub.audio,
            "motion": self.motion.moving(),
            "motion_level": round(self.motion.level * 100, 1),
            "recording": self.recorder.current,
            "qualities": list(QUALITIES),
        }
        self.send_body(json.dumps(body, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def snapshot(self):
        frame, _ = self.hub.video.wait_frame(self.hub.video.seq - 1, timeout=5)
        if frame is None:
            self.send_body("Камера недоступна\n".encode(), "text/plain; charset=utf-8",
                           HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if self.query.get("small"):
            img = Image.open(io.BytesIO(frame))
            img.draft("RGB", (img.width // 4, img.height // 4))
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=75)
            frame = out.getvalue()
        name = time.strftime("webcam-%Y%m%d-%H%M%S.jpg")
        self.send_body(frame, "image/jpeg",
                       extra={"Content-Disposition": f'inline; filename="{name}"'})

    def stream(self):
        quality = self.query.get("q", "1080p")
        transcoder = self.transcoders.get(quality)
        if quality not in QUALITIES:
            self.send_error(HTTPStatus.NOT_FOUND, "нет такого качества")
            return
        channel = transcoder.channel if transcoder else self.hub.video
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.hub.viewers += 1
        if transcoder:
            transcoder.acquire()
        try:
            seq = 0
            while True:
                frame, seq = channel.wait_frame(seq, timeout=5)
                if frame is None:
                    if transcoder:
                        transcoder.acquire()  # пережатие могло выключиться — включим снова
                        transcoder.release()
                    continue
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    b"Content-Length: %d\r\n\r\n" % len(frame)
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        finally:
            self.hub.viewers -= 1
            if transcoder:
                transcoder.release()

    def audio(self):
        """Живой звук: сырой PCM S16LE моно, страница проигрывает его через Web Audio."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Sample-Rate", str(AUDIO_RATE))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = self.hub.listen()
        try:
            while True:
                try:
                    chunk = q.get(timeout=5)
                except queue.Empty:
                    continue
                self.wfile.write(chunk)
        finally:
            self.hub.unlisten(q)

    def recordings(self):
        root = self.recorder.dir
        days = {}
        total = 0
        for p in sorted(root.rglob("*.mp4"), reverse=True):
            size = p.stat().st_size
            total += size
            rel = p.relative_to(root)
            days.setdefault(rel.parent.as_posix(), []).append({
                "time": p.stem.replace("-", ":"),
                "path": rel.as_posix(),
                "thumb": thumb_path(rel).as_posix() if thumb_path(p).exists() else None,
                "event": self.recorder.events.get(rel.as_posix()),
                "size": size,
            })
        body = json.dumps({
            "today": datetime.now().strftime("%Y-%m-%d"),  # по часам Pi: имена роликов в них же
            "total": total,
            "limit": self.recorder.args.max_storage_gb * 2**30,
            "clip": self.recorder.args.clip,
            "preroll": self.recorder.args.preroll,
            "days": [{"day": d, "clips": c} for d, c in days.items()],
        }, ensure_ascii=False).encode()
        self.send_body(body, "application/json; charset=utf-8")

    def recording_file(self, rel):
        root = self.recorder.dir.resolve()
        path = (root / rel).resolve()
        types = {".mp4": "video/mp4", ".jpg": "image/jpeg"}
        if not path.is_relative_to(root) or path.suffix not in types or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        start, end, status = 0, size - 1, HTTPStatus.OK
        # Range нужен браузеру, чтобы перематывать видео.
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(size - int(m.group(2)), 0)
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", types[path.suffix])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(left, 1 << 16))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


# ---------- запуск ----------

def parse_size(value):
    m = re.fullmatch(r"(\d+)x(\d+)", value)
    if not m:
        raise argparse.ArgumentTypeError("размер в виде 1280x720")
    return int(m.group(1)), int(m.group(2))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="0.0.0.0", help="адрес для прослушивания (0.0.0.0)")
    p.add_argument("--port", type=int, default=45461, help="порт панели (45461)")
    p.add_argument("--adguard-port", type=int, default=45463,
                   help="порт, на котором панель отдаёт AdGuard Home")
    p.add_argument("--ha-port", type=int, default=45462,
                   help="порт, на котором панель отдаёт Home Assistant (45462)")
    p.add_argument("--size", type=parse_size, default=(1920, 1080), help="разрешение (1920x1080)")
    p.add_argument("--fps", type=int, default=30, help="кадров в секунду (30)")
    p.add_argument("--device", help="конкретный /dev/videoN вместо автопоиска")
    p.add_argument("--audio-device", help="ALSA-устройство микрофона вместо автопоиска (hw:3,0)")
    p.add_argument("--no-audio", action="store_true", help="не захватывать звук")
    p.add_argument("--auth", default=os.environ.get("WEBCAM_AUTH"),
                   help="логин:пароль для входа на страницу (или env WEBCAM_AUTH)")
    p.add_argument("--record-dir", type=Path, default=HERE / "recordings",
                   help="куда складывать ролики (./recordings)")
    p.add_argument("--clip", type=float, default=60, help="длина ролика, сек (60)")
    p.add_argument("--preroll", type=float, default=15,
                   help="сколько секунд до начала движения попадает в ролик (15)")
    p.add_argument("--motion-area", type=float, default=1.0,
                   help="какой процент кадра должен измениться, чтобы считать движением (1.0)")
    p.add_argument("--bitrate", type=parse_bitrate, default="8M",
                   help="битрейт видео в записи (8M ≈ 60 МБ/мин)")
    p.add_argument("--max-storage-gb", type=float, default=20,
                   help="лимит папки с роликами, старые удаляются (20)")
    p.add_argument("--mqtt", default="localhost:1883",
                   help="брокер MQTT для датчиков в Home Assistant (localhost:1883)")
    p.add_argument("--no-mqtt", action="store_true", help="не публиковать датчики в MQTT")
    p.add_argument("--on-clip", default=os.environ.get("WEBCAM_ON_CLIP"),
                   help="команда, которой передаётся путь к каждому готовому ролику")
    p.add_argument("--list", action="store_true", help="показать найденные камеры и выйти")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
    )

    if args.list:
        cams = find_usb_cameras()
        if not cams:
            print("USB-камер не найдено")
        for cam in cams:
            print(f"{cam['device']}: {cam['name']}, микрофон: {find_mic(cam['device']) or 'нет'}")
            for fourcc, sizes in cam["formats"].items():
                print(f"    {fourcc}: {', '.join(f'{w}x{h}' for w, h in sizes)}")
        return

    hub = Hub(preroll=args.preroll)
    motion = MotionDetector(hub, area=args.motion_area / 100)
    recorder = Recorder(hub, motion, args)
    for thread in (Capture(hub, args), motion, recorder):
        thread.start()
    ha = None
    if not args.no_mqtt:
        host, _, port = args.mqtt.partition(":")
        ha = HAMqtt(hub, motion, recorder, host, int(port or 1883))
        recorder.listeners.append(ha.clip_saved)
        ha.start()

    Handler.hub, Handler.motion, Handler.recorder = hub, motion, recorder
    Handler.sysmon = SystemMonitor(disk_path=str(HERE))
    Handler.modem = HiLink()
    Handler.zerotier = ZeroTier()
    Handler.wifi = Hotspot()
    Handler.bt = Bluetooth()
    Handler.sysmon.start()
    Handler.transcoders = {
        name: Transcoder(hub, name, *preset)
        for name, preset in QUALITIES.items() if preset
    }
    auth = Auth(args.auth, store=HERE / "sessions.json")
    Handler.auth, Handler.ha_port = auth, args.ha_port
    panel.HAProxyHandler.auth, panel.HAProxyHandler.panel_port = auth, args.port
    ha_server = ThreadingHTTPServer((args.host, args.ha_port), panel.HAProxyHandler)
    ha_server.daemon_threads = True
    threading.Thread(target=ha_server.serve_forever, daemon=True, name="ha-proxy").start()
    Handler.adguard, Handler.adguard_port = AdGuard(), args.adguard_port
    Handler.sms = SmsAlerts(Handler.modem)
    panel.AdGuardProxyHandler.auth, panel.AdGuardProxyHandler.panel_port = auth, args.port
    adguard_server = ThreadingHTTPServer((args.host, args.adguard_port), panel.AdGuardProxyHandler)
    adguard_server.daemon_threads = True
    threading.Thread(target=adguard_server.serve_forever, daemon=True, name="adguard-proxy").start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log.info("Панель: http://%s:%d/, Home Assistant: порт %d, ролики: %s",
             args.host, args.port, args.ha_port, args.record_dir)

    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    # systemctl stop/restart шлёт SIGTERM: сначала дописываем начатый ролик, потом выходим.
    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    if recorder.current:
        log.info("Остановка: дописываю %s", recorder.current)
    recorder.stop(timeout=45)
    if ha and ha.client.is_connected():
        # Брокер получает сообщения по порядку: дошёл «offline» — дошёл и webcam/clip
        # дописанного ролика. Завещание брокера после выхода скажет то же самое.
        try:
            ha.client.publish(f"{TOPIC}/status", "offline", qos=1, retain=True).wait_for_publish(5)
        except (RuntimeError, ValueError):
            pass
    recorder.wait_hooks(timeout=30)  # вместе с 45 с выше укладываемся в 90 с systemd


if __name__ == "__main__":
    main()
