# network — как подключиться к Pi

Pi сама раздаёт Wi-Fi и принимает прямой кабель. Интернет у неё один — LTE-модем (`../modem`).

```
LTE-модем (eth1) ──► Pi ─┬─► Wi-Fi «PiHome» (wlan0): интернет + панель
                         └─► кабель в Ethernet-порт (eth0): только Pi и панель
```

| Как подключиться | Адрес Pi | Панель | Что получает устройство |
|---|---|---|---|
| Wi-Fi **PiHome** | `10.20.0.1`, имя `pi.lan` | http://10.20.0.1:45461 или http://pi.lan:45461 | адрес, шлюз и DNS (AdGuard Home, блокирует рекламу) — интернет через модем |
| Кабель в порт Pi | `10.10.0.1` | http://10.10.0.1:45461 | только адрес: видна Pi и панель, свой интернет устройства не трогаем |

- Home Assistant — тот же адрес, порт **45462**, AdGuard Home — порт **45463** (см. `../adguard`). SSH — `ssh dosash@10.10.0.1` или `ssh dosash@10.20.0.1`.
- На Mac и iPhone по любому каналу работает и `raspberry.local` (Bonjour).
- Кабель — просто вставить: адрес устройство получит само по DHCP через пару секунд.

## Как устроено

- **NetworkManager**, оба профиля в режиме `shared`: NM сам запускает для них dnsmasq (DHCP и DNS)
  и NAT.
  - `lan-cable` — `eth0`, `10.10.0.1/24`, адреса клиентам `.10–.254`.
  - `hotspot` — `wlan0`, точка доступа 2,4 ГГц, канал 6, WPA2 (CCMP, PMF выключен — встроенный
    Wi-Fi Pi с ним капризничает), `10.20.0.1/24`.
- `dnsmasq-shared.conf` (установлен в `/etc/NetworkManager/dnsmasq-shared.d/pi-network.conf`):
  DNS для устройств в Wi-Fi — AdGuard Home на `10.20.0.1:53` (`../adguard`), а dnsmasq держит свой DNS
  на порту 5354 только для локального (`pi.lan`, имена устройств) — у него AdGuard и спрашивает их.
  На кабеле не выдаём шлюз и DNS. Иначе компьютер, у которого кабель в списке служб выше Wi-Fi
  (так у Mac по умолчанию), пустил бы весь свой интернет через LTE-модем. Там же имя `pi.lan`.
- `hotspot-forward.sh` + `hotspot-forward.service`: Docker ставит политику `FORWARD DROP`, и без
  правил в цепочке `DOCKER-USER` устройства в точке доступа получали бы адрес, но не интернет.
  Сервис привязан к `docker.service` и перезапускается вместе с ним.
- Домашний Wi-Fi (профили `wifi-5g`, `wifi-24`) сохранён, но автоподключение выключено.
- Сторож `internet-failover` видит, что Wi-Fi раздаёт сеть, и не ищет через него интернет.
  Панель в доске «Интернет» показывает точку доступа (сколько устройств) и кабель.

## Пароль и имя сети

Проще всего — в панели: главная → блок **Wi-Fi** → имя сети, пароль, диапазон, канал → «Сохранить».
Там же видно, какие устройства подключены. Из командной строки:

```sh
sudo nmcli -s -g 802-11-wireless-security.psk connection show hotspot     # показать пароль
sudo nmcli connection modify hotspot wifi-sec.psk 'новый-пароль'          # сменить (от 8 символов)
sudo nmcli connection modify hotspot 802-11-wireless.ssid 'Имя'           # переименовать сеть
sudo nmcli connection up hotspot                                          # применить
```

Применение отключит всех от Wi-Fi Pi — делайте это по кабелю.

## Управление

```sh
nmcli device                                           # что на каком интерфейсе
/usr/sbin/iw dev wlan0 station dump                    # кто подключён к точке доступа
sudo cat /var/lib/NetworkManager/dnsmasq-wlan0.leases  # выданные адреса (и dnsmasq-eth0.leases)
sudo iptables -S DOCKER-USER                           # правила пересылки для точки доступа
sudo ss -lunp | grep -E ':53 |:5354 '                  # DNS: AdGuard на 53, dnsmasq на 5354
journalctl -u NetworkManager -f                        # подключения, ошибки точки доступа
```

## Вернуть Pi в домашний Wi-Fi

По кабелю (по Wi-Fi связь оборвётся):

```sh
sudo nmcli connection modify hotspot connection.autoconnect no
sudo nmcli connection modify wifi-5g connection.autoconnect yes
sudo nmcli connection modify wifi-24 connection.autoconnect yes
sudo nmcli connection up wifi-5g
```

Pi снова станет `192.168.3.240`, а сторож вернётся к схеме «Wi-Fi основной, модем резерв».
Обратно — те же команды наоборот и `sudo nmcli connection up hotspot`.

## Создать заново (после переустановки)

```sh
sudo install -m 644 dnsmasq-shared.conf /etc/NetworkManager/dnsmasq-shared.d/pi-network.conf
sudo nmcli connection add type ethernet ifname eth0 con-name lan-cable \
  connection.autoconnect-priority 100 ipv4.method shared ipv4.addresses 10.10.0.1/24 ipv6.method link-local
sudo nmcli connection add type wifi ifname wlan0 con-name hotspot ssid PiHome \
  connection.autoconnect-priority 100 802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses 10.20.0.1/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  wifi-sec.pmf disable wifi-sec.psk 'ПАРОЛЬ'
sudo install -m 644 hotspot-forward.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hotspot-forward
```

Нужны пакеты `network-manager`, `dnsmasq-base`, `iw`. Регион Wi-Fi — `cfg80211.ieee80211_regdom=RU`
в `/boot/firmware/cmdline.txt`.

## Файлы

| Файл | Что это |
|---|---|
| `dnsmasq-shared.conf` | DHCP: без шлюза на кабеле, имя `pi.lan`; установлен в `/etc/NetworkManager/dnsmasq-shared.d/` |
| `hotspot-forward.sh` | правила `DOCKER-USER` для точки доступа |
| `hotspot-forward.service` | юнит, установлен в `/etc/systemd/system/` |

После правки `dnsmasq-shared.conf` (установить его, как выше) dnsmasq перечитывает настройки только
при новом подключении профиля: `sudo nmcli connection up hotspot` (Wi-Fi-клиенты отключатся на пару секунд)
и `sudo nmcli connection up lan-cable`.

Профили NetworkManager лежат в `/etc/NetworkManager/system-connections/` (`lan-cable`, `hotspot`).
