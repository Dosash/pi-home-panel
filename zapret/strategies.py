"""Стратегии flowseal/zapret-discord-youtube → аргументы nfqws для Linux.

Стратегия flowseal — .bat, который запускает winws.exe (это nfqws, собранный под Windows) с набором
профилей через --new. Опции у них общие; только --wf-tcp/--wf-udp (фильтр WinDivert: какие порты
отдавать winws) на Linux заменяет nftables — из них берутся NFQWS_PORTS_TCP/UDP для zapret.

Переменные .bat подставляются так же, как это делает service.bat:
  %BIN%    → bin/ релиза flowseal (заготовки пакетов *.bin);
  %LISTS%  → lists/ релиза, а свои списки (*-user.txt) и ipset-all.txt — из локальной папки,
             чтобы обновление стратегий их не затирало;
  %GameFilter% / %GameFilterTCP% / %GameFilterUDP% → порты игрового фильтра или 12, если он выключен
             (как у flowseal: профиль на порт 12 ни с чем не совпадает).
"""
import re
from pathlib import Path

GAME_PORTS = "1024-65535"
GAME_OFF = "12"
GAME_MODES = ("off", "tcp", "udp", "all")
USER_LISTS = ("list-general-user.txt", "list-exclude-user.txt", "ipset-exclude-user.txt")
LOCAL_LISTS = USER_LISTS + ("ipset-all.txt",)  # ipset-all.txt зависит от режима IPSet — тоже свой

# Только такие аргументы попадают в конфиг zapret (его читает shell от root): никаких пробелов,
# кавычек, $, ` и прочего — даже если в .bat окажется что-то странное.
SAFE_ARG = re.compile(r"^--[a-z0-9][a-z0-9-]*(=[A-Za-z0-9_.,:/@+=~^!-]*)?$")  # ^! — встроенная заготовка nfqws
PORT_LIST = re.compile(r"^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$")


# winws у flowseal бывает новее nfqws из релиза zapret: такие значения заменяем ближайшими
# и помечаем стратегию (note) — в панели видно, что она отличается от оригинала.
COMPAT = {
    "--dpi-desync-fake-tls=^!": ("--dpi-desync-fake-tls=!", "«^!» → «!»: стандартная заготовка TLS без пересборки"),
}


class StrategyError(ValueError):
    pass


def winws_command(text):
    """Аргументы winws.exe из текста .bat (строки, склеенные по ^, кавычки сняты)."""
    text = re.sub(r"\^\r?\n", " ", text.replace("\r\n", "\n"))
    lines = [line for line in text.splitlines() if "winws.exe" in line and not line.lstrip().startswith(("::", "rem "))]
    if len(lines) != 1:
        raise StrategyError(f"ожидался один запуск winws.exe, найдено {len(lines)}")
    tail = lines[0].split("winws.exe", 1)[1].lstrip('"').strip()
    return re.findall(r'"[^"]*"|[^\s"]+(?:"[^"]*")?[^\s"]*', tail)


def game_ports(mode):
    if mode not in GAME_MODES:
        raise StrategyError(f"неизвестный режим игрового фильтра: {mode}")
    tcp = GAME_PORTS if mode in ("tcp", "all") else GAME_OFF
    udp = GAME_PORTS if mode in ("udp", "all") else GAME_OFF
    return {"%GameFilter%": tcp, "%GameFilterTCP%": tcp, "%GameFilterUDP%": udp}


def ports(value):
    """«80,443,12,%GameFilterTCP%» после подстановки → «80,443» (порт-заглушку 12 nftables не нужен)."""
    items = [p for p in value.split(",") if p and p != GAME_OFF]
    out = ",".join(dict.fromkeys(items))
    if out and not PORT_LIST.match(out):
        raise StrategyError(f"странный список портов: {value}")
    return out


def convert(text, release, local, game="off"):
    """Текст .bat → {"tcp": порты, "udp": порты, "profiles": [[аргументы профиля], ...], "notes": [...]}."""
    release, local = Path(release), Path(local)
    subst = {"%BIN%": f"{release / 'bin'}/", **game_ports(game)}
    tcp = udp = ""
    profiles, current, notes = [], [], []
    for raw in winws_command(text):
        arg = raw.replace('"', "")
        for name in LOCAL_LISTS:  # свои списки — из локальной папки
            arg = arg.replace(f"%LISTS%{name}", str(local / name))
        arg = arg.replace("%LISTS%", f"{release / 'lists'}/")
        for key, value in subst.items():
            arg = arg.replace(key, value)
        if "%" in arg:  # осталась переменная .bat, которой мы не знаем
            raise StrategyError(f"не знаю, что подставить в {raw}")
        if arg.startswith("--wf-tcp="):
            tcp = ports(arg.split("=", 1)[1])
        elif arg.startswith("--wf-udp="):
            udp = ports(arg.split("=", 1)[1])
        elif arg.startswith("--wf-"):
            continue  # прочие фильтры WinDivert на Linux не нужны
        elif arg == "--new":
            if current:
                profiles.append(current)
            current = []
        else:
            if arg in COMPAT:
                arg, note = COMPAT[arg]
                if note not in notes:
                    notes.append(note)
            if not SAFE_ARG.match(arg):
                raise StrategyError(f"недопустимый аргумент: {arg}")
            current.append(arg)
    if current:
        profiles.append(current)
    if not profiles:
        raise StrategyError("в стратегии нет профилей")
    return {"tcp": tcp, "udp": udp, "profiles": profiles, "notes": notes}


def strategy_name(path):
    """«general (ALT2).bat» → «general (ALT2)»."""
    return Path(path).stem


def sort_key(name):
    """general, ALT, ALT2 … ALT13, потом остальные семьи — как их привыкли перечислять."""
    m = re.match(r"^general(?: \((.*?)(\d*)\))?$", name)
    if not m:
        return (2, name, 0)
    family, num = (m.group(1) or ""), m.group(2)
    return (0 if family in ("", "ALT") else 1, family, int(num or 1) if family else 0)


def load(release, local, game="off"):
    """Все стратегии релиза: [{"name", "tcp", "udp", "profiles"}] или с "error", если не разобралась."""
    out = []
    for bat in Path(release).glob("general*.bat"):
        item = {"name": strategy_name(bat)}
        try:
            item.update(convert(bat.read_text(encoding="utf-8", errors="replace"), release, local, game))
        except StrategyError as e:
            item["error"] = str(e)
        out.append(item)
    return sorted(out, key=lambda s: sort_key(s["name"]))


def nfqws_opt(profiles):
    """Профили → значение NFQWS_OPT (по строке на профиль, как в config.default zapret)."""
    return "\n" + " --new\n".join(" ".join(p) for p in profiles) + "\n"
