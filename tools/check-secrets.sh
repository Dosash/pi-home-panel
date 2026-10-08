#!/usr/bin/env bash
# Проверка перед пушем в публичный репозиторий: нет ли в том, что уйдёт на GitHub, настоящих секретов.
#
#   tools/check-secrets.sh            — коммиты, которых ещё нет на GitHub, плюс незакоммиченное
#   tools/check-secrets.sh <ревизия>  — всё, что изменилось с этой ревизии
#
# Значения берутся с Pi (пароль панели, AdGuard, Wi-Fi, токен и сети ZeroTier, ключ Zigbee, номера SMS)
# и живут только в памяти этого скрипта; на экран — лишь длина найденного. Пароль SSH на Pi не хранится —
# его можно передать в SSH_PASSWORD, чтобы проверить и его.
set -euo pipefail
cd "$(dirname "$0")/.."

base="${1:-origin/main}"
git fetch -q origin 2>/dev/null || echo "(GitHub недоступен — сравниваю с последним известным origin/main)"
# добавленные строки (закоммиченное и нет) и новые файлы целиком
added=$( { git diff "$base" -- . | grep '^+' | grep -v '^+++' ;
           git ls-files --others --exclude-standard -z | xargs -0 cat 2>/dev/null ; } || true)

values=$(tools/pi.sh '
  cd ~/project
  grep -h -E "^#? *WEBCAM_AUTH=" webcam/webcam.env 2>/dev/null | grep -v "логин:пароль" | sed -E "s/^#? *WEBCAM_AUTH=[^:]*://"
  sed -n "s/^ADGUARD_AUTH=[^:]*://p" adguard/adguard.env 2>/dev/null
  sudo nmcli -s -g 802-11-wireless-security.psk connection show hotspot 2>/dev/null
  sudo cat /var/lib/zerotier-one/authtoken.secret 2>/dev/null; echo
  sudo zerotier-cli listnetworks 2>/dev/null | awk "NR>1{print \$3}"
  python3 -c "import json; print(\"\n\".join(json.load(open(\"alerts/settings.json\"))[\"phones\"]))" 2>/dev/null
  sudo python3 -c "
import yaml
k = (yaml.safe_load(open(\"zigbee/data/configuration.yaml\")).get(\"advanced\") or {}).get(\"network_key\")
print(\"zigbee-key:\" + (\",\".join(map(str, k)) if isinstance(k, list) else str(k or \"\")))" 2>/dev/null
' | grep -v -E '^\s*$')
[ -n "${SSH_PASSWORD:-}" ] && values=$(printf '%s\n%s' "$values" "$SSH_PASSWORD")

count=0 hits=0
squashed=$(printf '%s' "$added" | tr -d ' \n')
while IFS= read -r v; do
  [ -z "$v" ] && continue
  count=$((count + 1))
  case "$v" in
    zigbee-key:*)  # список чисел в YAML — ищем как последовательность без пробелов и переводов строк
      key=${v#zigbee-key:}
      [ -n "$key" ] && printf '%s' "$squashed" | grep -qF -- "$key" && { echo "!!! ключ Zigbee-сети"; hits=$((hits + 1)); } ;;
    *)
      # короткое ищем только целым словом: иначе четыре цифры пароля совпадут с частью номера порта
      if [ ${#v} -lt 8 ]; then flags=-qwF; else flags=-qF; fi
      printf '%s' "$added" | grep $flags -- "$v" && { echo "!!! найден секрет (${#v} симв.)"; hits=$((hits + 1)); } ;;
  esac
done <<< "$values"

phones=$(printf '%s' "$added" | grep -o -E '\+?[78][ (-]*9[0-9]{2}[ )-]*[0-9]{3}[ -]*[0-9]{2}[ -]*[0-9]{2}' \
  | grep -v -E '000[ -]?00[ -]?0[0-9]|123[ -]?45[ -]?67' | sort -u || true)

echo "проверено значений с Pi: $count"
[ -n "$phones" ] && { echo "!!! похоже на номера телефонов (проверьте, не настоящие ли):"; echo "$phones" | sed 's/^/    /'; }
if [ $hits -gt 0 ]; then
  echo "НЕ ПУШИТЬ: найдено секретов — $hits"
  exit 1
fi
echo "секретов не найдено"
