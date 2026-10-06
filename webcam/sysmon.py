"""Состояние Raspberry Pi для панели: температура, загрузка CPU, память, диск, сеть, процессы.

Замер раз в INTERVAL секунд в отдельном потоке; история хранится в памяти.
Всё читается из /proc и /sys, кроме троттлинга (vcgencmd).
"""
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("webcam")

INTERVAL = 3  # сек между замерами
HISTORY = 200  # замеров в истории (10 минут)
TOP_PROCESSES = 5
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# Биты vcgencmd get_throttled: «сейчас» и «было с момента загрузки».
THROTTLE_NOW = {0: "undervoltage", 1: "freq_capped", 2: "throttled", 3: "soft_temp_limit"}
THROTTLE_PAST = {16: "undervoltage", 17: "freq_capped", 18: "throttled", 19: "soft_temp_limit"}


def read(path, default=None):
    # Имена процессов — произвольные байты: ядро режет comm до 15 байт, может и посреди
    # русской буквы. Строгий UTF-8 тут уронил бы весь мониторинг.
    try:
        return Path(path).read_bytes().decode(errors="replace")
    except OSError:
        return default


def cpu_times():
    """{"cpu": (busy, total), "cpu0": ...} из /proc/stat, в тиках."""
    out = {}
    for line in (read("/proc/stat") or "").splitlines():
        if not line.startswith("cpu"):
            break
        name, *vals = line.split()
        vals = [int(v) for v in vals]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals[:8])  # guest уже учтён в user
        out[name] = (total - idle, total)
    return out


def temperature():
    raw = read("/sys/class/thermal/thermal_zone0/temp")
    return round(int(raw) / 1000, 1) if raw else None


def cpu_freq():
    cur = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    top = read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    return (int(cur) // 1000 if cur else None, int(top) // 1000 if top else None)


def memory():
    info = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0]) * 1024
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    return {
        "total": total,
        "used": total - avail,
        "swap_total": info.get("SwapTotal", 0),
        "swap_used": info.get("SwapTotal", 0) - info.get("SwapFree", 0),
    }


def throttling():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout
        bits = int(out.strip().split("=")[1], 16)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None
    return {
        "now": [name for bit, name in THROTTLE_NOW.items() if bits >> bit & 1],
        "since_boot": [name for bit, name in THROTTLE_PAST.items() if bits >> bit & 1],
    }


def net_bytes():
    rx = tx = 0
    for line in (read("/proc/net/dev") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith(("docker", "veth", "br-")):
            continue
        vals = rest.split()
        rx += int(vals[0])
        tx += int(vals[8])
    return rx, tx


def process_label(pid, comm):
    """Понятное имя процесса: интерпретаторы подписываем по тому, что они запускают."""
    cmd = (read(f"/proc/{pid}/cmdline", "") or "").replace("\0", " ").strip()
    if "homeassistant" in cmd:
        return "Home Assistant"
    if "webcam.py" in cmd:
        return "Панель и камера"
    if comm == "node":
        try:
            if "zigbee2mqtt" in os.readlink(f"/proc/{pid}/cwd"):
                return "Zigbee2MQTT"
        except OSError:
            pass
    return comm


def process_ticks():
    """{pid: (метка, тики CPU, RSS в байтах)} по всем процессам."""
    out = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        stat = read(f"/proc/{entry.name}/stat")
        statm = read(f"/proc/{entry.name}/statm")
        if not stat or not statm:
            continue
        # comm в скобках может содержать пробелы и скобки — режем по последней «)».
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2:].split()
        ticks = int(fields[11]) + int(fields[12])  # utime + stime
        rss = int(statm.split()[1]) * PAGE_SIZE
        out[int(entry.name)] = (comm, ticks, rss)
    return out


class SystemMonitor(threading.Thread):
    def __init__(self, disk_path="/"):
        super().__init__(daemon=True, name="sysmon")
        self.disk_path = disk_path
        self.lock = threading.Lock()
        self.history = deque(maxlen=HISTORY)  # (unix time, °C, CPU %)
        self.current = None
        self.labels = {}  # pid → понятное имя (cmdline читаем один раз на процесс)

    def run(self):
        prev = None  # (cpu, net, процессы, время) прошлого замера
        next_at = time.monotonic()
        last_error = float("-inf")  # первую ошибку пишем в журнал сразу
        while True:
            # Ровный шаг: 200 замеров — ровно 10 минут, сколько и показывает график.
            # пропущенные такты (завис на долгом замере) не догоняем пачкой
            next_at = max(next_at + INTERVAL, time.monotonic())
            time.sleep(max(0.0, next_at - time.monotonic()))
            try:
                prev = self.sample(prev)
            except Exception:  # мониторинг не должен ронять панель
                prev = None
                if time.monotonic() - last_error > 300:
                    log.exception("Мониторинг Pi: замер не удался")
                    last_error = time.monotonic()

    def sample(self, prev):
        now_t = time.monotonic()
        cpu, net, procs = cpu_times(), net_bytes(), process_ticks()
        if prev is None:
            return cpu, net, procs, now_t  # первый замер только запоминает счётчики
        prev_cpu, prev_net, prev_proc, prev_t = prev
        dt = now_t - prev_t

        def usage(name):
            busy0, total0 = prev_cpu.get(name, (0, 0))
            busy1, total1 = cpu.get(name, (0, 0))
            return round(100 * (busy1 - busy0) / (total1 - total0), 1) if total1 > total0 else 0.0

        cores = sorted((n for n in cpu if n != "cpu"), key=lambda n: int(n[3:]))
        top = []
        for pid, (comm, ticks, rss) in procs.items():
            if pid not in prev_proc or prev_proc[pid][0] != comm:
                continue
            pct = 100 * (ticks - prev_proc[pid][1]) / CLK_TCK / dt
            top.append((pct, pid, comm, rss))
        top.sort(reverse=True)
        self.labels = {pid: name for pid, name in self.labels.items() if pid in procs}
        for _, pid, comm, _ in top[:TOP_PROCESSES]:
            if pid not in self.labels:
                self.labels[pid] = process_label(pid, comm)

        freq, freq_max = cpu_freq()
        disk = shutil.disk_usage(self.disk_path)
        temp = temperature()
        total_cpu = usage("cpu")
        uptime = float((read("/proc/uptime") or "0").split()[0])
        current = {
            "time": time.time(),
            "monotonic": now_t,
            "temperature": temp,
            "cpu": total_cpu,
            "cores": [usage(n) for n in cores],
            "load": [round(x, 2) for x in os.getloadavg()],
            "freq": freq,
            "freq_max": freq_max,
            "memory": memory(),
            # used + free, а не total: как в df, без блоков, зарезервированных для root
            "disk": {"total": disk.used + disk.free, "used": disk.used},
            "net": {"rx": max(0, (net[0] - prev_net[0]) / dt),
                    "tx": max(0, (net[1] - prev_net[1]) / dt)},
            "throttling": throttling(),
            "uptime": uptime,
            "processes": [
                {"name": self.labels.get(pid, comm), "pid": pid, "cpu": round(pct, 1), "rss": rss}
                for pct, pid, comm, rss in top[:TOP_PROCESSES]
            ],
        }
        with self.lock:
            self.current = current
            self.history.append((round(current["time"], 1), temp, total_cpu))
        return cpu, net, procs, now_t

    def snapshot(self):
        with self.lock:
            current = self.current
            return {
                "interval": INTERVAL,
                # Сколько секунд назад был последний замер: страница покажет, если данные застыли.
                "age": round(time.monotonic() - current["monotonic"], 1) if current else None,
                "current": current,
                "history": list(self.history),
            }
