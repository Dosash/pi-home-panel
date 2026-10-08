# Как дорабатывать проект

Код работает на Raspberry Pi (`~/project` там — рабочая копия этого же репозитория), а правится
в локальной копии на компьютере. Изменения едут на Pi скриптом `tools/deploy.sh`, проверяются там,
и только потом — коммит и пуш на GitHub. Что где устроено — в `README.md` и README каждой папки.

## Доступ к Pi

| | |
|---|---|
| Wi-Fi «PiHome» | Pi — `10.20.0.1` (`pi.lan`) |
| Кабель (USB-сетевая карта → `eth0` Pi) | Pi — `10.10.0.1`; шлюз и DNS по кабелю не выдаются нарочно |
| Пользователь | `dosash`, `sudo` без пароля |
| Интернет у Pi | только LTE-модем Huawei E3372 (`eth1`); своих часов у Pi нет — время верное после NTP |

```sh
tools/pi.sh                          # SSH на Pi (ssh -4, соединение живёт 10 минут)
tools/pi.sh 'systemctl --failed'     # команда на Pi
PI_HOST=dosash@10.10.0.1 tools/pi.sh # по кабелю
```

Вход по ключу вместо пароля (один раз): `ssh-keygen -t ed25519` (если ключа ещё нет), затем
`ssh-copy-id -o AddressFamily=inet dosash@10.20.0.1`.

**Интернет на компьютере, пока он в Wi-Fi Pi.** Когда в модеме нет SIM, у PiHome нет интернета.
Чтобы не терять его (и связь с ИИ-ассистентом), раздайте интернет с телефона по USB и поставьте
«iPhone USB» выше Wi-Fi: Системные настройки → Сеть → «…» → Порядок служб.

## Локальная копия

```sh
git clone https://github.com/Dosash/pi-home-panel.git pi_home && cd pi_home
git remote add pi ssh://dosash@10.20.0.1/home/dosash/project   # чтобы забирать то, что закоммичено на Pi
git config user.name Dosash
git config user.email 33052625+Dosash@users.noreply.github.com
```

Пуш на GitHub — с правами `gh` (`gh auth login`): в этой копии
`git config credential.helper '!gh auth git-credential'`.

## Цикл работы

1. **Правка** локально. Тесты: `tools/test.sh` (только `python3`, без пакетов; работают и на Mac).
2. **На Pi**: `tools/deploy.sh --dry-run` — что поедет (всё, что отличается от коммита на Pi);
   `tools/deploy.sh --restart` — выложить и перезапустить затронутые службы. Файлы `*.service`
   и `network/` скрипт не применяет — подскажет, что сделать руками.
3. **Проверка на Pi**: страница в браузере, `tools/pi.sh 'journalctl -u <служба> -e'`.
4. **Коммит** локально, затем `tools/check-secrets.sh` — и только если «секретов не найдено»:
   `git push origin main`.
5. **Pi догоняет GitHub**: `tools/pi.sh 'cd ~/project && git pull --ff-only'`. Если у Pi сейчас нет
   интернета: `git push pi main:from-mac`, затем `tools/pi.sh 'cd ~/project && git merge --ff-only from-mac'`.

Если на Pi что-то закоммитили напрямую: `git fetch pi && git merge --ff-only pi/main`.

## Службы на Pi

| Служба | Что | Порт | Код |
|---|---|---|---|
| `webcam` | главная панель: камера, Wi-Fi, Bluetooth, AdGuard, модем, SMS, мониторинг; прокси HA и AdGuard | 45461 (HA 45462, AdGuard 45463) | `webcam/` |
| `sms-alerts` | SMS-оповещения через модем | — | `alerts/` |
| `zapret` | обход блокировок (zapret, nfqws, nftables) | — | `/opt/zapret`, конфиг пишет `zapret/control.py` |
| `zapret-panel` | панель zapret | 45464 | `zapret/` |
| `internet-failover` | сторож резервного канала | — | `modem/` |
| `hotspot-forward` | правила DOCKER-USER для Wi-Fi | — | `network/` |
| `AdGuardHome` | DNS для Wi-Fi | 53, UI 127.0.0.1:3080 | `/opt/AdGuardHome`, `adguard/` |
| `homeassistant` (Docker) | Home Assistant | 127.0.0.1:8123 | `homeassistant/` |
| `zigbee2mqtt` | Zigbee, когда вставлен адаптер | 127.0.0.1:8099 | `zigbee/` |
| `mosquitto` | MQTT | 127.0.0.1:1883 | — |

## Правила

**Секреты — никогда в репозиторий** (он публичный). Пароли, ключи и номера живут только на Pi
и перечислены в `README.md` → «Что не в репозитории»; новые — сразу в `.gitignore` и туда же
в таблицу, с образцом `*.example`. Перед каждым пушем — `tools/check-secrets.sh`. В примерах
и тестах — только заглушки (`+7 000 000-00-00`, `ПАРОЛЬ`, адреса из `203.0.113.0/24`).

**Безопасность панелей.** Входа нет — панели видны только в локальной сети и ZeroTier. Поэтому:
отвечать только на свой IP и локальные имена (`panel.host_allowed` — защита от DNS rebinding),
команды — только `POST` с JSON (обычная форма с чужого сайта так не отправит), действия от root —
через `sudo -n` конкретных команд, а всё, что попадает в конфиги, которые читает root (zapret,
NetworkManager), — проверять по белому списку.

**Выкладка — атомарно** (`tools/deploy.sh` так и делает): файл сначала во временную папку, потом `mv`.
Иначе панель может отдать браузеру наполовину записанную страницу.

**Сеть** меняется так, чтобы не остаться без связи с Pi: изменения точки доступа — скриптом, запущенным
на самой Pi через `systemd-run`, с автооткатом, если точка не поднялась. SSH до Pi часто идёт через
ту же Wi-Fi и на время перезапуска пропадает. Службу `nftables` не включать — она сбрасывает все правила
(Docker, NetworkManager, zapret).

**zapret.** Не перезапускать `zapret-panel`, пока идёт проверка или подбор (deploy-скрипт это
проверяет). Проверки стратегий изолированы от Wi-Fi: свой nfqws на очереди 201 и правило nftables
только для своих запросов (`meta skuid`, метка `0x20000000` — как у рабочего правила zapret).

**Мелочи, на которых уже обжигались.** `pkill -f` по строке, которая есть в той же SSH-команде,
убивает саму сессию. В `tar` с Mac — `COPYFILE_DISABLE=1`, иначе на Pi появятся файлы `._*`.

## Стиль

- Всё для людей — по-русски: интерфейс, README, комментарии, сообщения коммитов. Имена в коде — английские.
- Python — только стандартная библиотека и то, что уже есть в Debian (PyGObject, paho-mqtt, numpy,
  Pillow, GStreamer). Без фреймворков: `http.server`, `subprocess`, `json`.
- Страницы — один HTML-файл без сборки и внешних скриптов, тёмная тема на CSS-переменных
  (`--bg`, `--panel`, `--accent`…), обновление опросом `fetch`.
- Комментарии объясняют «почему», а не пересказывают код. README папки обновляется вместе с кодом.
- Новая логика — с тестами в `<папка>/tests/` (`unittest`, без сети и без Pi).
- Коммиты: автор `Dosash <33052625+Dosash@users.noreply.github.com>`; если помогал ИИ —
  строка `Co-Authored-By:` в конце сообщения.
