"""zapret на Pi: настройки, конфиг /opt/zapret/config, запуск и остановка, состояние.

zapret (bol-van, /opt/zapret) сам ставит правила nftables и запускает nfqws по своему конфигу —
мы только пишем этот конфиг из выбранной стратегии flowseal и настроек и перезапускаем службу.
Права root — через sudo -n (у пользователя панели он без пароля, как у Wi-Fi в панели).
"""
import copy
import functools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import strategies

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "settings.json"  # в .gitignore — состояние этой Pi
ZAPRET = Path("/opt/zapret")
CONFIG = ZAPRET / "config"
NFQWS = ZAPRET / "nfq/nfqws"
FLOWSEAL = Path("/opt/zapret-flowseal")
RELEASE = FLOWSEAL / "current"  # ссылка на папку текущей версии стратегий
LOCAL = FLOWSEAL / "local"  # свои списки и ipset-all.txt: обновление стратегий их не трогает
SERVICE = "zapret"
IPSET_NONE = "203.0.113.113/32"  # адрес из документационной сети: «ни с чем не совпадает», как у flowseal
IPSET_MODES = ("none", "loaded", "any")
PLACEHOLDERS = {  # как создаёт service.bat: пустой список nfqws не принимает
    "list-general-user.txt": "# Never leave this file empty\ndomain.example.abc\n",
    "list-exclude-user.txt": "domain.example.abc\n",
    "ipset-exclude-user.txt": IPSET_NONE + "\n",
}
DEFAULTS = {"enabled": True, "strategy": "general", "game": "off", "ipset": "none"}
DOMAIN = re.compile(r"^\^?(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.?$")
NET = re.compile(r"^[0-9a-f:.]+(/\d{1,3})?$")
MAX_LIST = 2000  # строк в своём списке


def run(*cmd, timeout=30, input=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)


def sudo(*cmd, timeout=60, input=None):
    r = run("sudo", "-n", *cmd, timeout=timeout, input=input)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                           else f"{cmd[0]}: код {r.returncode}")
    return r.stdout


# ---------- настройки ----------

def load_settings():
    try:
        data = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        data = {}
    out = copy.deepcopy(DEFAULTS)
    out.update({k: v for k, v in data.items() if k in DEFAULTS})
    return out


def save_settings(settings):
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, SETTINGS)


def validate(settings, names):
    s = {**load_settings(), **{k: v for k, v in settings.items() if k in DEFAULTS}}
    s["enabled"] = bool(s["enabled"])
    if s["strategy"] not in names:
        raise ValueError(f"нет такой стратегии: {s['strategy']}")
    if s["game"] not in strategies.GAME_MODES:
        raise ValueError("игровой фильтр: off, tcp, udp или all")
    if s["ipset"] not in IPSET_MODES:
        raise ValueError("IPSet: none, loaded или any")
    return s


# ---------- свои списки ----------

def read_list(name):
    try:
        lines = (LOCAL / name).read_text().splitlines()
    except OSError:
        return ""
    return "\n".join(line for line in lines
                     if line.strip() and not line.startswith("#") and line.strip() not in ("domain.example.abc", IPSET_NONE))


def write_list(name, text):
    """Свой список из панели: проверяем каждую строку, пустой — заменяем заглушкой flowseal."""
    if name not in strategies.USER_LISTS:
        raise ValueError(f"нет такого списка: {name}")
    lines = [line.strip().lower() for line in str(text).replace(",", "\n").splitlines() if line.strip()]
    if len(lines) > MAX_LIST:
        raise ValueError(f"не больше {MAX_LIST} строк")
    check = NET if name.startswith("ipset") else DOMAIN
    for line in lines:
        if line.startswith("http"):
            raise ValueError(f"нужен домен без http:// — «{line}»")
        if not check.match(line):
            raise ValueError(f"«{line}» — не {'адрес или подсеть' if check is NET else 'домен'}")
    body = "\n".join(dict.fromkeys(lines)) + "\n" if lines else PLACEHOLDERS[name]
    (LOCAL / name).write_text(body)


def ensure_local(ipset_mode):
    """Свои списки (если их нет) и ipset-all.txt по режиму IPSet — как его переключает service.bat."""
    for name, body in PLACEHOLDERS.items():
        if not (LOCAL / name).exists():
            (LOCAL / name).write_text(body)
    target = LOCAL / "ipset-all.txt"
    if ipset_mode == "none":
        target.write_text(IPSET_NONE + "\n")
    elif ipset_mode == "any":
        target.write_text("")  # пустой список — любые адреса
    else:
        shutil.copyfile(RELEASE / "lists/ipset-all.txt.backup", target)


# ---------- конфиг и служба ----------

def release_version():
    try:
        return (RELEASE / ".service/version.txt").read_text().strip()
    except OSError:
        return RELEASE.resolve().name


@functools.lru_cache(maxsize=1)
def zapret_version():
    r = run(str(NFQWS), "--version", timeout=5)
    m = re.search(r"version (v[\d.]+)", r.stdout + r.stderr)
    return m.group(1) if m else None


def load_strategies(game="off"):
    return strategies.load(RELEASE, LOCAL, game)


def build_config(strategy):
    """Конфиг zapret: nfqws на исходящих соединениях (Wi-Fi и сама Pi), nftables, без IPv6.

    IFACE_WAN не задан: правила на любом выходе, а локальные адреса flowseal и так исключает
    (ipset-exclude.txt) — так обход переживёт смену интерфейса модема или Wi-Fi-клиента как WAN."""
    return f"""# Создан панелью zapret (~/project/zapret) — ручные правки перезапишутся.
# Стратегия: {strategy['name']} (flowseal {release_version()})
FWTYPE=nftables
SET_MAXELEM=522288
IPSET_OPT="hashsize 262144 maxelem $SET_MAXELEM"
AUTOHOSTLIST_RETRANS_THRESHOLD=3
AUTOHOSTLIST_FAIL_THRESHOLD=3
AUTOHOSTLIST_FAIL_TIME=60
AUTOHOSTLIST_DEBUGLOG=0
GZIP_LISTS=1
DESYNC_MARK=0x40000000
DESYNC_MARK_POSTNAT=0x20000000
TPWS_SOCKS_ENABLE=0
TPWS_ENABLE=0
NFQWS_ENABLE=1
NFQWS_PORTS_TCP={strategy['tcp']}
NFQWS_PORTS_UDP={strategy['udp']}
NFQWS_TCP_PKT_OUT=$((6+$AUTOHOSTLIST_RETRANS_THRESHOLD))
NFQWS_TCP_PKT_IN=3
NFQWS_UDP_PKT_OUT=$((6+$AUTOHOSTLIST_RETRANS_THRESHOLD))
NFQWS_UDP_PKT_IN=0
NFQWS_OPT="{strategies.nfqws_opt(strategy['profiles'])}"
MODE_FILTER=none
FLOWOFFLOAD=donttouch
INIT_APPLY_FW=1
DISABLE_IPV6=1
FILTER_TTL_EXPIRED_ICMP=1
"""


def dry_run(strategy):
    args = [a for p in strategy["profiles"] for a in p + ["--new"]][:-1]
    r = run(str(NFQWS), "--dry-run", "--qnum=200", *args, timeout=15)
    if r.returncode:
        raise ValueError("nfqws не принял стратегию: " + ((r.stderr or r.stdout).strip().splitlines() or ["?"])[-1])


def apply(settings):
    """Пишет конфиг под настройки и (пере)запускает zapret — или останавливает, если он выключен."""
    found = {s["name"]: s for s in load_strategies(settings["game"])}
    strategy = found.get(settings["strategy"])
    if strategy is None or "error" in strategy:
        raise ValueError(strategy["error"] if strategy else f"нет стратегии {settings['strategy']}")
    ensure_local(settings["ipset"])
    dry_run(strategy)
    sudo("tee", str(CONFIG), input=build_config(strategy))
    if settings["enabled"]:
        sudo("systemctl", "enable", SERVICE)
        sudo("systemctl", "restart", SERVICE, timeout=60)
    else:
        sudo("systemctl", "disable", "--now", SERVICE, timeout=60)
    save_settings(settings)


def service_state():
    active = run("systemctl", "is-active", SERVICE, timeout=5).stdout.strip()
    enabled = run("systemctl", "is-enabled", SERVICE, timeout=5).stdout.strip()
    pids = run("pgrep", "-x", "nfqws", timeout=5).stdout.split()
    return {"active": active, "enabled": enabled == "enabled", "nfqws": len(pids)}


def journal(lines=40):
    try:
        return sudo("journalctl", "-u", SERVICE, "-n", str(lines), "--no-pager", "-o", "short-iso", timeout=10)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return f"журнал недоступен: {e}"
