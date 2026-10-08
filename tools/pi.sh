#!/usr/bin/env bash
# SSH на Pi: tools/pi.sh                  — войти
#            tools/pi.sh 'команда'        — выполнить и выйти
#
# Адрес — PI_HOST (по умолчанию Wi-Fi «PiHome»; по кабелю: PI_HOST=dosash@10.10.0.1).
# -4 обязателен: VPN на Mac однажды ломал ssh по IPv6. Соединение переиспользуется 10 минут,
# так что пароль спрашивается один раз (лучше настроить вход по ключу — см. CONTRIBUTING.md).
set -euo pipefail

PI_HOST="${PI_HOST:-dosash@10.20.0.1}"
mkdir -p "$HOME/.ssh"
exec ssh -4 -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/pi-%C" -o ControlPersist=10m \
  -o ServerAliveInterval=15 "$PI_HOST" "$@"
