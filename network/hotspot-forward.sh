#!/bin/sh
# Пропускает трафик устройств из точки доступа (wlan0) в интернет.
# Docker ставит политику FORWARD DROP, и без этого они получили бы адрес, но не интернет.
# Правила — в цепочке DOCKER-USER: её Docker создаёт, но никогда не очищает.
set -e
iptables -N DOCKER-USER 2>/dev/null || true
add() { iptables -C DOCKER-USER "$@" 2>/dev/null || iptables -I DOCKER-USER "$@"; }
add -o wlan0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
add -i wlan0 -j ACCEPT
