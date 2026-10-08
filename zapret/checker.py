"""Проверка обхода: открываются ли YouTube, Discord и прочие цели flowseal — и с какой стратегией.

Цели — utils/targets.txt релиза flowseal. Адреса берём у AdGuard (как устройства в Wi-Fi: DNS
оператора YouTube не отдаёт) и подставляем curl через --resolve. Каждая цель — TLS 1.2 и TLS 1.3,
с загрузкой первых 64 КБ: так видно и блокировку при соединении, и обрыв на 16–20 КБ, которым
ТСПУ режет «замедленные» сайты.

Стратегии-кандидаты проверяются изолированно: отдельный nfqws на очереди CHECK_QUEUE и правило
nftables только для трафика этой проверки (meta skuid — пользователь, от которого она идёт).
Включённая стратегия и трафик устройств в Wi-Fi при этом не меняются.
"""
import concurrent.futures as cf
import os
import random
import re
import socket
import struct
import subprocess
import time

import control

CHECK_QUEUE = 201
CHECK_TAG = "zapret-check"  # комментарий на временных правилах: по нему они и убираются
TIMEOUT, CONNECT_TIMEOUT = 6, 4  # сек на загрузку и на соединение
RANGE = 65535  # первые 64 КБ
PAST_CUT = 24 * 1024  # дальше 16–20 КБ пришло — это не обрыв ТСПУ, просто медленно (большая страница по LTE)
PARALLEL = 32  # все проверки разом: ~6 с на стратегию
TLS = {"TLS1.2": ["--tlsv1.2", "--tls-max", "1.2"], "TLS1.3": ["--tlsv1.3"]}
FALLBACK_TARGETS = [
    ("DiscordMain", "https://discord.com"), ("DiscordGateway", "https://gateway.discord.gg"),
    ("DiscordCDN", "https://cdn.discordapp.com"), ("YouTubeWeb", "https://www.youtube.com"),
    ("YouTubeImage", "https://i.ytimg.com"), ("YouTubeVideoRedirect", "https://redirector.googlevideo.com"),
    ("GoogleMain", "https://www.google.com"), ("CloudflareWeb", "https://www.cloudflare.com"),
]


def targets():
    """[(имя, "https://…" или "PING:1.2.3.4")] из targets.txt релиза flowseal."""
    try:
        text = (control.RELEASE / "utils/targets.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return FALLBACK_TARGETS
    found = re.findall(r'^\s*(\w+)\s*=\s*"((?:https?://|PING:)[^"\s]+)"', text, re.M)
    return found or FALLBACK_TARGETS


def resolve(name, server="127.0.0.1", timeout=4):
    """A-запись через AdGuard (минимальный DNS-клиент: в Debian нет dig по умолчанию)."""
    tid = random.randint(0, 65535)
    query = (struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
             + b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
             + struct.pack(">HH", 1, 1))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(query, (server, 53))
        data = s.recv(4096)
    if struct.unpack(">H", data[:2])[0] != tid or data[3] & 15:
        return None
    count, i = struct.unpack(">H", data[6:8])[0], 12
    while data[i]:
        i += data[i] + 1
    i += 5
    for _ in range(count):
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i]:
                i += data[i] + 1
            i += 1
        rtype, _cls, _ttl, length = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if rtype == 1:
            return socket.inet_ntoa(data[i:i + 4])
        i += length
    return None


def classify(code, exit_code, size, stderr):
    """Итог одной загрузки: ok, blocked (ничего не пришло), cut (обрыв на середине), ssl, error."""
    if exit_code == 0 or (exit_code == 18 and size > 0):  # 18 — сервер отдал меньше, чем просили
        return "ok"
    if exit_code == 28 and size >= PAST_CUT:  # сервер не умеет Range и шлёт всю страницу — не успела
        return "ok"
    if re.search(r"certificate|SSL certificate|self[- ]?signed", stderr, re.I):
        return "ssl"  # чужой сертификат — подмена (DNS или MITM)
    if exit_code == 28:
        return "cut" if size > 0 else "blocked"
    if exit_code in (35, 56, 52, 7):  # сброс при рукопожатии, обрыв, пустой ответ, нет соединения
        return "cut" if size > 0 else "blocked"
    return "error"


def check_url(url, tls, ip):
    host = url.split("/")[2]
    port = 443 if url.startswith("https") else 80
    cmd = ["curl", "-s", "-o", "/dev/null", "-r", f"0-{RANGE}", "-m", str(TIMEOUT),
           "--connect-timeout", str(CONNECT_TIMEOUT), *TLS[tls], "--resolve", f"{host}:{port}:{ip}",
           "-w", "%{http_code} %{size_download} %{time_total}", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 5)
    code, size, took = (r.stdout.split() + ["000", "0", "0"])[:3]
    return {"status": classify(code, r.returncode, int(size), r.stderr), "code": int(code),
            "kb": round(int(size) / 1024), "time": round(float(took), 1)}


def ping(addr):
    r = subprocess.run(["ping", "-c", "1", "-W", "2", addr], capture_output=True, text=True, timeout=5)
    m = re.search(r"time=([\d.]+)", r.stdout)
    return {"status": "ok" if r.returncode == 0 else "blocked", "ms": round(float(m.group(1))) if m else None}


def run_suite(items=None):
    """Проверка всех целей: [{"name", "url", "TLS1.2": {...}, "TLS1.3": {...}} | {"name", "ping": {...}}]."""
    items = items or targets()
    ips = {}
    for _, url in items:
        if not url.startswith("PING:"):
            host = url.split("/")[2]
            if host not in ips:
                try:
                    ips[host] = resolve(host)
                except OSError:
                    ips[host] = None
    jobs = []
    for name, url in items:
        if url.startswith("PING:"):
            jobs.append((name, url, "ping", None))
        else:
            jobs += [(name, url, tls, ips[url.split("/")[2]]) for tls in TLS]
    results = {}
    with cf.ThreadPoolExecutor(PARALLEL) as ex:
        futures = {}
        for name, url, kind, ip in jobs:
            if kind == "ping":
                futures[ex.submit(ping, url[5:])] = (name, url, "ping")
            elif ip is None:
                results.setdefault(name, {"name": name, "url": url})[kind] = {"status": "dns"}
            else:
                futures[ex.submit(check_url, url, kind, ip)] = (name, url, kind)
        for f in cf.as_completed(futures):
            name, url, kind = futures[f]
            try:
                value = f.result()
            except Exception as e:  # curl завис сильнее таймаута и т.п. — это тоже «не работает»
                value = {"status": "error", "error": str(e)}
            results.setdefault(name, {"name": name, "url": url})[kind] = value
    return [results[name] for name, _ in items if name in results]


def score(rows):
    """Сколько проверок TLS прошло — по нему сравниваются стратегии (как в тестах flowseal)."""
    return sum(1 for row in rows for tls in TLS if row.get(tls, {}).get("status") == "ok")


def total(rows):
    return sum(1 for row in rows for tls in TLS if tls in row)


# ---------- изолированная проверка стратегии ----------

def _nft(*args, input=None):
    return control.sudo("nft", *args, input=input, timeout=15)


def clear_rules():
    """Убирает временные правила проверки (в том числе оставшиеся после сбоя)."""
    for table, chain in (("zapret", "postnat"), ("zapret_check", "postnat")):
        try:
            listing = _nft("-a", "list", "chain", "inet", table, chain)
        except RuntimeError:
            continue
        for handle in re.findall(rf'comment "{CHECK_TAG}".*# handle (\d+)', listing):
            _nft("delete", "rule", "inet", table, chain, "handle", handle)
    try:
        _nft("delete", "table", "inet", "zapret_check")
    except RuntimeError:
        pass


def add_rules(strategy):
    """Очередь CHECK_QUEUE для трафика этой проверки. Если zapret включён — правило в его же цепочке
    первым (queue завершает обход цепочки, так пакет не попадёт ещё и в рабочий nfqws); если нет —
    своя таблица на том же месте в postrouting."""
    uid = os.getuid()
    rules = []
    if strategy["tcp"]:
        rules.append(f"tcp dport {{ {strategy['tcp'].replace(',', ', ')} }}")
    if strategy["udp"]:
        rules.append(f"udp dport {{ {strategy['udp'].replace(',', ', ')} }}")
    try:
        _nft("list", "chain", "inet", "zapret", "postnat")
        table = "zapret"
    except RuntimeError:
        table = "zapret_check"
        _nft("-f", "-", input=f"""table inet zapret_check {{
  chain postnat {{ type filter hook postrouting priority srcnat + 1; policy accept; }}
}}""")
    for match in rules:
        # mark & 0x40000000 — пакеты самого nfqws (fwmark) обратно не отдаём. Метку 0x20000000
        # nfqws переносит на свои пакеты, и zapret выводит их из conntrack — как в рабочем правиле.
        _nft("insert", "rule", "inet", table, "postnat", "meta", "skuid", str(uid),
             "meta", "mark", "&", "0x40000000", "==", "0", *match.split(),
             "ct", "original", "packets", "1-9", "meta", "mark", "set", "meta", "mark", "|", "0x20000000",
             "queue", "flags", "bypass", "to", str(CHECK_QUEUE), "comment", f'"{CHECK_TAG}"')


def check_strategy(strategy, items=None):
    """Запускает nfqws со стратегией на очереди проверки, гоняет цели, всё убирает."""
    args = [a for p in strategy["profiles"] for a in p + ["--new"]][:-1]
    clear_rules()
    proc = subprocess.Popen(["sudo", "-n", str(control.NFQWS), f"--qnum={CHECK_QUEUE}",
                             "--dpi-desync-fwmark=0x40000000", *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(0.8)  # пусть nfqws прочтёт списки и займёт очередь
        if proc.poll() is not None:
            raise RuntimeError("nfqws не запустился: " + (proc.stderr.read().strip().splitlines() or ["?"])[-1])
        add_rules(strategy)
        return run_suite(items)
    finally:
        clear_rules()
        proc.terminate()  # sudo передаст сигнал nfqws
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
