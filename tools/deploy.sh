#!/usr/bin/env bash
# Выкладка из локальной копии на Pi (~/project) — каждый файл атомарно: сначала во временную папку,
# потом mv на место. Иначе панель может отдать браузеру наполовину записанную страницу.
#
#   tools/deploy.sh                 — всё, что отличается от коммита, который сейчас на Pi
#                                     (закоммиченное и нет, плюс новые файлы)
#   tools/deploy.sh файл…           — только эти файлы
#   tools/deploy.sh --restart …     — и перезапустить службы, которых касаются файлы
#   tools/deploy.sh --dry-run …     — только показать, что поедет
#
# Удалённые файлы не удаляются на Pi — это вручную. *.service и network/ — тоже вручную (см. вывод).
set -euo pipefail
cd "$(dirname "$0")/.."
PI="tools/pi.sh"

restart=0 dry=0 files=()
for arg in "$@"; do
  case "$arg" in
    --restart) restart=1 ;;
    --dry-run) dry=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) files+=("$arg") ;;
  esac
done

if [ ${#files[@]} -eq 0 ]; then
  pi_head=$($PI 'git -C ~/project rev-parse HEAD')
  if ! git cat-file -e "$pi_head^{commit}" 2>/dev/null; then
    echo "На Pi коммит $pi_head, которого нет здесь: git fetch pi && git merge pi/main — потом выкладка." >&2
    exit 1
  fi
  while IFS= read -r f; do files+=("$f"); done < <(
    { git diff --name-only "$pi_head"; git ls-files --others --exclude-standard; } | sort -u)
fi

existing=()
for f in ${files[@]+"${files[@]}"}; do
  if [ -f "$f" ]; then existing+=("$f"); else echo "пропускаю (нет файла): $f"; fi
done
if [ ${#existing[@]} -eq 0 ]; then
  echo "Нечего выкладывать: на Pi то же самое."
  exit 0
fi

echo "На Pi поедет (${#existing[@]}):"
printf '  %s\n' "${existing[@]}"
[ $dry -eq 1 ] && exit 0

# COPYFILE_DISABLE и --no-mac-metadata: без служебных ._файлов macOS
COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -cf - "${existing[@]}" | $PI '
  set -e
  T=$(mktemp -d)
  tar xf - -C "$T" 2>/dev/null
  cd "$T"
  find . -type f | while read -r f; do
    mkdir -p "$HOME/project/$(dirname "$f")"
    mv -f "$f" "$HOME/project/$f"
  done
  rm -rf "$T"'
echo "Готово."

# Какие службы задевают файлы
services=() manual=()
for f in "${existing[@]}"; do
  case "$f" in
    *.service)                 manual+=("$f — скопировать в /etc/systemd/system/ и sudo systemctl daemon-reload") ;;
    network/*)                 manual+=("$f — сеть меняется вручную по network/README.md") ;;
    webcam/tests/*|alerts/tests/*|zapret/tests/*|*.md) ;;
    webcam/*)                  services+=(webcam) ;;
    alerts/*)                  services+=(sms-alerts) ;;
    zapret/*)                  services+=(zapret-panel) ;;
    modem/failover.py)         services+=(internet-failover) ;;
  esac
done
for m in ${manual[@]+"${manual[@]}"}; do echo "вручную: $m"; done
[ ${#services[@]} -eq 0 ] && exit 0
to_restart=$(printf '%s\n' "${services[@]}" | sort -u | tr '\n' ' ')

if [ $restart -eq 0 ]; then
  echo "Перезапустить: tools/pi.sh 'sudo systemctl restart $to_restart'  (или --restart)"
  exit 0
fi
for s in $to_restart; do
  if [ "$s" = zapret-panel ] && $PI 'curl -s http://127.0.0.1:45464/api/status' 2>/dev/null \
       | grep -q '"state": "running"'; then
    echo "zapret-panel занят (идёт проверка или подбор) — перезапустите позже: tools/pi.sh 'sudo systemctl restart zapret-panel'"
    continue
  fi
  $PI "sudo systemctl restart $s && sleep 2 && echo \"$s: \$(systemctl is-active $s)\""
done
