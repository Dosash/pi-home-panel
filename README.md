# pi-home-panel — домашний сервер на Raspberry Pi

Raspberry Pi 4 (Debian 13) как домашний хаб: раздаёт свой Wi-Fi с интернетом от LTE-модема,
блокирует рекламу, пишет видео по движению, собирает датчики (Bluetooth, Zigbee) в Home Assistant,
шлёт SMS, когда пропадал свет, стало холодно или камера заметила движение, и обходит блокировки
YouTube и Discord для всех устройств в своём Wi-Fi (zapret).
Всё — из одной веб-панели без входа: она видна только в локальной сети и через ZeroTier.

```
LTE-модем ──eth1──► Raspberry Pi ──wlan0──► Wi-Fi «PiHome»: интернет, панель, DNS с AdGuard
                         │        ──eth0───► кабель: только Pi и панель
                         ├─ панель :45461 ─► Home Assistant :45462, AdGuard Home :45463, Zigbee2MQTT /z2m
                         ├─ zapret (nfqws) — обход DPI для трафика из Wi-Fi, своя панель :45464
                         ├─ USB-камера, Bluetooth (BlueZ), Zigbee-адаптер (когда вставлен)
                         └─ ZeroTier — доступ извне
```

## Как выглядит

![Главная: плитки сервисов и трей со свёрнутыми разделами](docs/screenshots/home.png)

| ![Wi-Fi](docs/screenshots/wifi.png) | ![AdGuard Home](docs/screenshots/adguard.png) |
|:---:|:---:|
| Wi-Fi: устройства в сети, QR-код для подключения, настройки | AdGuard Home: блокировки за сутки, график, топы |
| ![Интернет](docs/screenshots/internet.png) | ![Raspberry Pi](docs/screenshots/system.png) |
| Интернет: LTE-модем и доступ извне через ZeroTier | Мониторинг Pi: температура, процессор, память, процессы |

| ![SMS-оповещения](docs/screenshots/sms.png) |
|:---:|
| SMS-оповещения: пропало питание, холодно у датчика, движение у камеры — номера, что включено и датчики прямо в панели |

<details>
<summary>📱 На телефоне</summary>
<br>
<p align="center"><img src="docs/screenshots/mobile.png" width="260" alt="Панель на телефоне"></p>
</details>

Камера, LTE-модем, устройства в Wi-Fi и раздел SMS на скриншотах — демонстрационные данные (камера не
подключена, SIM не вставлена, номер — заглушка); личное — QR-код с паролем Wi-Fi, ID сети ZeroTier, адреса устройств, часть статистики
AdGuard — размыто.

## Части

| Папка | Что это |
|---|---|
| [`webcam/`](webcam/README.md) | веб-панель: камера с записью по движению, Wi-Fi (устройства, QR-код, настройки), Bluetooth и BLE-датчики, AdGuard, интернет и модем, мониторинг Pi |
| [`network/`](network/README.md) | сеть Pi: точка доступа «PiHome», кабель, DHCP и локальные имена |
| [`modem/`](modem/README.md) | LTE-модем Huawei и сторож резервного канала |
| [`alerts/`](alerts/README.md) | SMS-оповещения через модем: пропадало питание, холодно у датчика, движение у камеры — настройка в панели |
| [`adguard/`](adguard/README.md) | AdGuard Home: блокировка рекламы, шифрованный DNS наружу |
| [`zapret/`](zapret/README.md) | обход блокировок YouTube и Discord для Wi-Fi: zapret со стратегиями flowseal, панель с проверкой и подбором стратегии |
| [`homeassistant/`](homeassistant/README.md) | Home Assistant в Docker |
| [`zigbee/`](zigbee/README.md) | Zigbee2MQTT, запускается сам, когда вставлен адаптер |

## Адреса

| | Wi-Fi «PiHome» | Кабель |
|---|---|---|
| Панель | http://10.20.0.1:45461 или http://pi.lan:45461 | http://10.10.0.1:45461 |
| Home Assistant | http://10.20.0.1:45462 | http://10.10.0.1:45462 |
| AdGuard Home | http://10.20.0.1:45463 | http://10.10.0.1:45463 |
| Панель zapret | http://10.20.0.1:45464 (кнопка «Настройка zapret» в шапке панели) | http://10.10.0.1:45464 |
| SSH | `ssh dosash@10.20.0.1` | `ssh dosash@10.10.0.1` |

## Что не в репозитории

Секреты и данные остаются на Pi (см. `.gitignore`). Храните их копию отдельно от репозитория.

| Где | Что | Образец |
|---|---|---|
| `webcam/webcam.env` | пароль панели (сейчас вход выключен) | `webcam/webcam.env.example` |
| `webcam/sessions.json` | сессии входа в панель | — |
| `adguard/adguard.env` | пароль администратора AdGuard Home | `adguard/adguard.env.example` |
| `zigbee/data/` | настройки Zigbee2MQTT **с ключом Zigbee-сети** — потеряете, датчики придётся сопрягать заново | `zigbee/configuration.example.yaml` |
| `homeassistant/config/` | всё, кроме своего YAML: база, журналы, токены (`.storage`) | — |
| `webcam/recordings/` | записи камеры | — |
| профиль NetworkManager `hotspot` | пароль Wi-Fi «PiHome»: `sudo nmcli -s -g 802-11-wireless-security.psk connection show hotspot` | `network/README.md` |
| `alerts/settings.json` | номера телефонов для SMS и что включено (меняется в панели, раздел «SMS») | — |
| `homeassistant/.env` | часовой пояс (`TZ=…`) для контейнера HA | — |
| `/opt/AdGuardHome/AdGuardHome.yaml` | настройки AdGuard Home (описаны в `adguard/README.md`) | — |
| `/opt/zapret`, `/opt/zapret-flowseal` | zapret и стратегии flowseal — скачиваются при установке; свои списки — в `/opt/zapret-flowseal/local` | `zapret/README.md` |
| `zapret/settings.json`, `zapret/state.json` | выбранная стратегия и настройки обхода; итоги проверок | — |

## Установка на чистую Pi

По порядку, в каждой папке README с командами: `network` (точка доступа и кабель) → `modem`
(резервный канал) → `webcam` (systemd-юнит панели, пакеты GStreamer) → `homeassistant`
(`docker compose up -d`) → `zigbee` → `adguard` → `alerts` → `zapret`.

## Лицензия

MIT — см. `LICENSE`. Исключение: `webcam/vendor/segno` — библиотека segno, BSD-3-Clause (`webcam/vendor/segno/LICENSE`).
[zapret](https://github.com/bol-van/zapret) и стратегии [flowseal](https://github.com/flowseal/zapret-discord-youtube)
в репозиторий не входят — скачиваются при установке, у них свои лицензии.

## Тесты

```sh
cd webcam && python3 -m unittest discover -s tests   # расшифровка BLE-датчиков
cd alerts && python3 -m unittest discover -s tests   # SMS-оповещения: питание, температура
cd zapret && python3 -m unittest discover -s tests   # стратегии flowseal → nfqws, списки, проверка
```
