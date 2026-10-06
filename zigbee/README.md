# zigbee

Zigbee2MQTT для датчиков Zigbee. Открывается из панели: `http://10.20.0.1:45461/z2m/`
(плитка «Zigbee» на главной).

- Программа: `/opt/zigbee2mqtt` (git, версия 2.14.1). Данные и настройки: `data/`.
- Брокер MQTT: Mosquitto на `localhost:1883` (пакет `mosquitto`, слушает только localhost).
- Устройства сами появляются в Home Assistant (`homeassistant.enabled: true`), если в HA
  добавлена интеграция MQTT с брокером `localhost`.

## Адаптер

Порт и тип адаптера определяются автоматически: Sonoff ZBDongle-E / -P, SMLIGHT SLZB,
ConBee и другие, которые поддерживает zigbee-herdsman.

Пока Zigbee-адаптер не вставлен, сервис не запускается (`ExecCondition` в юните) и не тратит CPU.
Как только его вставят, правило udev `99-zigbee-adapter.rules` запустит Zigbee2MQTT сам.

Какие устройства считаются Zigbee-адаптерами, решает `adapters.py`: он берёт список USB VID:PID
прямо из установленного zigbee-herdsman. Поэтому другие USB-устройства с последовательным портом
(например, 4G-модем Huawei) Zigbee2MQTT не запускают.

```sh
./adapters.py            # известные адаптеры и какие из них вставлены
./adapters.py --check    # код 0, если адаптер вставлен (так проверяет сервис)
# после обновления Zigbee2MQTT — обновить правило udev:
./adapters.py --rules | sudo tee /etc/udev/rules.d/99-zigbee-adapter.rules && sudo udevadm control --reload-rules
```

Если автоопределение не справится, в `data/configuration.yaml` можно указать явно:

```yaml
serial:
  port: /dev/serial/by-id/usb-...   # ls /dev/serial/by-id/
  adapter: ember                    # ember (EFR32, ZBDongle-E), zstack (CC2652, ZBDongle-P), deconz (ConBee)
```

## Важно

`data/configuration.yaml` содержит ключ Zigbee-сети (`network_key`, `pan_id`, `ext_pan_id`),
сгенерированный при первом запуске. Если его потерять, все датчики придётся сопрягать заново.
Сохрани копию `data/` вместе с `coordinator_backup.json`, который появится после первого запуска с адаптером.

## Управление

```sh
sudo systemctl status zigbee2mqtt
journalctl -u zigbee2mqtt -f
```

## Файлы

| Файл | Что это |
|---|---|
| `data/configuration.yaml` | настройки (права 600) |
| `zigbee2mqtt.service` | юнит, установлен в `/etc/systemd/system/` |
| `adapters.py` | список Zigbee-адаптеров из zigbee-herdsman, проверка и генерация правила udev |
| `99-zigbee-adapter.rules` | правило udev (создано `adapters.py --rules`), установлено в `/etc/udev/rules.d/` |

## Обновление

```sh
cd /opt/zigbee2mqtt && git pull && pnpm i --frozen-lockfile && pnpm run build
cd ~/project/zigbee && ./adapters.py --rules | sudo tee /etc/udev/rules.d/99-zigbee-adapter.rules >/dev/null
sudo udevadm control --reload-rules && sudo systemctl restart zigbee2mqtt
```
