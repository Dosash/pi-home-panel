# zapret — обход блокировок для Wi-Fi Pi

YouTube, Discord и прочие сайты из списков открываются у всех устройств в Wi-Fi «PiHome» без
настроек на самих устройствах. Работает [zapret](https://github.com/bol-van/zapret) (nfqws) со
стратегиями из [flowseal/zapret-discord-youtube](https://github.com/flowseal/zapret-discord-youtube).

```
устройство в Wi-Fi ──DNS──► AdGuard Home (реклама; наружу — шифрованный DoH) ──► интернет
                   ──трафик──► Pi: nftables → nfqws (правит первые пакеты к сайтам из списков) ──► модем ──► интернет
```

DNS оператора YouTube не отдаёт, а DPI режет TLS-рукопожатие с Discord и YouTube. AdGuard решает
первое (адреса — по DoH), zapret — второе: nfqws подмешивает фейковые пакеты и режет ClientHello
так, что DPI не узнаёт сайт. Через nfqws идут только первые пакеты соединений на нужных портах
(80, 443, порты Discord); остальной трафик его не касается.

## Панель

Кнопка **«Настройка zapret»** в шапке главной панели → `http://10.20.0.1:45464` (отдельное окно).
Точка на кнопке: зелёная — обход работает, красная — zapret не запущен, серая — выключен.

- **Состояние**: вкл/выкл обхода, стратегия, итог последней проверки, версии zapret и стратегий,
  обновление стратегий с GitHub (кнопка появляется, когда у flowseal вышла новая версия).
- **Проверка** — как сейчас открываются цели flowseal (`utils/targets.txt`: Discord, YouTube, Google,
  Cloudflare) у устройств в Wi-Fi: TLS 1.2 и 1.3, первые 64 КБ. «блок» — соединение не прошло,
  «обрыв» — данные встали на середине (так ТСПУ режет на 16–20 КБ), «подмена» — чужой сертификат.
- **Стратегии** — все `general*.bat` flowseal, переведённые для nfqws. «Проверить» и
  **«Подобрать автоматически»** (все по очереди, ~3 минуты) идут **отдельно от Wi-Fi**: временный
  nfqws на очереди 201 и правило nftables только для запросов самой проверки (`meta skuid`) —
  включённая стратегия и устройства в сети ничего не замечают. «Включить» — применить стратегию.
- **Настройки** — как в `service.bat` у flowseal: игровой фильтр (off/tcp/udp/all) и IPSet
  (none — только списки доменов, loaded — плюс IP из списка flowseal, any — любые адреса).
- **Свои списки** — домены для обхода, исключения доменов и адресов.
- **Журнал** — `journalctl -u zapret`.

## Как устроено

| Где | Что |
|---|---|
| `/opt/zapret` | zapret v72.13 (бинарники сверены с `sha256sum.txt` релиза), служба `zapret` (`init.d/systemd/zapret.service`): ставит правила nftables и запускает nfqws по `/opt/zapret/config` |
| `/opt/zapret-flowseal/<версия>`, `current` → она | стратегии flowseal: `general*.bat`, `lists/`, `bin/*.bin` (заготовки пакетов). Windows-программы не берутся |
| `/opt/zapret-flowseal/local` | свои списки (`*-user.txt`) и `ipset-all.txt` по режиму IPSet — обновление стратегий их не трогает |
| `strategies.py` | `.bat` → профили nfqws: `--wf-tcp/--wf-udp` → порты для nftables, `%BIN%`/`%LISTS%`/игровой фильтр — как в `service.bat`. Каждый аргумент сверяется с белым списком символов: конфиг читает shell от root |
| `control.py` | настройки, генерация `/opt/zapret/config`, `nfqws --dry-run` перед применением, перезапуск службы (через `sudo -n`) |
| `checker.py` | проверка целей (адреса — через AdGuard, curl `--resolve`) и изолированная проверка стратегии |
| `web.py`, `page.html` | панель, служба `zapret-panel` (порт 45464, пользователь dosash) |
| `settings.json`, `state.json` | выбранная стратегия и настройки; итоги проверок (не в репозитории) |

Конфиг zapret: `FWTYPE=nftables`, правила на любом выходном интерфейсе (`IFACE_WAN` не задан —
локальные адреса flowseal и так исключает), без IPv6, `NFQWS_TCP_PKT_OUT=9` — nfqws видит только
первые пакеты соединения. Таблица nftables своя (`inet zapret`) и с правилами Docker
и NetworkManager не пересекается; служба `nftables` (она сбрасывает все правила) выключена.

**Совместимость.** winws у flowseal бывает новее nfqws из релиза zapret: `--dpi-desync-fake-tls=^!`
заменяется на `!` (стандартная заготовка), такие стратегии помечены в панели.

## Установка

```sh
# zapret
curl -LO https://github.com/bol-van/zapret/releases/download/v72.13/zapret-v72.13.tar.gz
curl -LO https://github.com/bol-van/zapret/releases/download/v72.13/sha256sum.txt
tar xzf zapret-v72.13.tar.gz && sha256sum -c --quiet sha256sum.txt
sudo mv zapret-v72.13 /opt/zapret && sudo /opt/zapret/install_bin.sh
sudo cp /opt/zapret/init.d/systemd/zapret.service /etc/systemd/system/
# стратегии flowseal
curl -LO https://github.com/flowseal/zapret-discord-youtube/releases/download/1.10.3/zapret-discord-youtube-1.10.3.tar.gz
sudo mkdir -p /opt/zapret-flowseal && sudo tar xzf zapret-discord-youtube-1.10.3.tar.gz -C /opt/zapret-flowseal
sudo mv /opt/zapret-flowseal/zapret-discord-youtube-1.10.3 /opt/zapret-flowseal/1.10.3
sudo rm -f /opt/zapret-flowseal/1.10.3/bin/*.{exe,dll,sys} && sudo ln -sfn 1.10.3 /opt/zapret-flowseal/current
sudo install -d -o dosash -g dosash /opt/zapret-flowseal/local
sudo apt install nftables            # нужна утилита nft; службу nftables не включать
# панель
sudo cp ~/project/zapret/zapret-panel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now zapret-panel
```

Дальше — в панели: «Подобрать автоматически», затем «Включить» у лучшей стратегии (это и запишет
конфиг, и включит службу `zapret`).

## Если что-то сломалось

```sh
sudo systemctl stop zapret            # весь трафик — напрямую, без обхода
journalctl -u zapret -u zapret-panel -e
sudo nft list table inet zapret       # правила zapret
cd ~/project/zapret && python3 -m unittest discover -s tests
```
